"""意图分类器：用一次 LLM 调用判断文本里是否包含可执行动作。"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger


# 不使用 LLM 的快速规则：匹配括号内的动作描述
# 覆盖"（发来一张XXX）""（分享一张XXX）""（发出一段XXX）"等模式
_FAST_IMAGE_RE = re.compile(
    r"[（(]"
    r"(?:发来|发了|分享|分享了|放了|附上|配上|发出|发送|递来|递了)"
    r"(?:了)?"
    r"[一]?[张幅]?"
    r"(?P<desc>[^）)]{4,120})"
    r"(?:的)?(?:照片|图片|特写|图|照|画面|截图|美图|自拍|照片)?"
    r"[，,。\s]*(?:[^）)]{0,30})?"
    r"[）)]",
    re.DOTALL,
)

# 宽松兜底：括号内含图片关键词
_LOOSE_IMAGE_RE = re.compile(
    r"[（(]([^）)]{4,150}(?:一张|张图|图片|照片|特写|画面|照片)[^）)]{0,60})[）)]",
    re.DOTALL,
)


class IntentClassifier:
    """意图分类器。

    两段式设计：
    1. 规则快速通道（零延迟）：正则匹配「动作括号」，直接提取 prompt
    2. LLM 慢通道（可选）：对无法规则匹配但语义上可能含有动作的文本做兜底分类
       - 开销大，默认只在规则失败时启用
       - 通过配置 classifier_provider_id 控制
    """

    # 工具名到执行器类型的映射（从 LLM 返回的 action 字段）
    ACTION_MAP = {
        "generate_image": "image",
        "draw": "image",
        "image": "image",
    }

    def __init__(self, context: Any, config: dict, tool_registry: Any) -> None:
        self.context = context
        self.config = config
        self.tool_registry = tool_registry

    # ── 公开接口 ──────────────────────────────────────────────────────────

    async def classify(
        self, text: str, source: str = "bot_outgoing"
    ) -> dict | None:
        """分析文本，返回需要执行的动作描述，或 None（无动作）。

        返回格式：
        {
            "action": "image",          # 动作类型
            "param": "具体的生图描述",   # 执行参数
            "clean_text": "去掉指令后的文本",  # 净化后的原文
            "method": "rule" | "llm",   # 命中路径
        }
        """
        # 快速规则通道
        result = self._rule_classify(text)
        if result:
            return result

        # 没有规则命中 → 是否启用 LLM 慢通道
        # 对于 bot_outgoing（主动消息），规则失败基本说明无动作，不再消耗 LLM
        # 对于 incoming（用户消息），可考虑 LLM 扫描（由 scan_incoming 配置控制）
        if source == "incoming" and self.config.get("tool_scan_enabled", False):
            return await self._llm_classify(text)

        return None

    # ── 规则分类 ──────────────────────────────────────────────────────────

    def _rule_classify(self, text: str) -> dict | None:
        """基于正则的零延迟规则分类。"""
        if not self.config.get("enable_image_generation", True):
            return None

        # 尝试用户自定义正则
        custom_pattern = (self.config.get("action_pattern") or "").strip()
        if custom_pattern:
            try:
                m = re.search(custom_pattern, text, re.DOTALL)
                if m:
                    desc = (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)).strip()
                    clean = text.replace(m.group(0), "").strip()
                    return {"action": "image", "param": desc, "clean_text": clean, "method": "rule_custom"}
            except re.error as e:
                logger.warning(f"[proactive_action] 自定义正则错误: {e}")

        # 内置精确规则
        for m in _FAST_IMAGE_RE.finditer(text):
            raw = m.group(0)
            inner = raw.strip("（()）").strip()
            # 去掉动词前缀
            inner = re.sub(
                r"^(?:发来|发了|分享|分享了|放了|附上|配上|发出|发送|递来|递了)(?:了)?[一]?[张幅]?",
                "", inner,
            ).strip()
            # 去掉末尾"语气XXX"等非图像描述性词
            inner = re.sub(r"[，,。\s]*语气\S+$", "", inner).strip("，,。 \t")
            # 去掉末尾括号内的说明（逗号后内容）
            inner = re.sub(r"[，,][^，,]{0,30}$", "", inner).strip("，,。 \t")
            if len(inner) < 3:
                continue
            clean_text = text.replace(raw, "").strip()
            return {
                "action": "image",
                "param": inner,
                "clean_text": clean_text,
                "method": "rule_exact",
            }

        # 宽松规则兜底
        for m in _LOOSE_IMAGE_RE.finditer(text):
            raw = m.group(0)
            inner = m.group(1).strip()
            if len(inner) < 4:
                continue
            clean_text = text.replace(raw, "").strip()
            return {
                "action": "image",
                "param": inner,
                "clean_text": clean_text,
                "method": "rule_loose",
            }

        return None

    # ── LLM 慢通道 ────────────────────────────────────────────────────────

    async def _llm_classify(self, text: str) -> dict | None:
        """用 LLM 做意图分类（慢通道，仅在规则失败且启用时调用）。"""
        try:
            provider_id = (self.config.get("classifier_provider_id") or "").strip()

            # 构建工具提示（动态扫描已注册工具）
            tool_hints = ""
            if self.config.get("tool_scan_enabled", False):
                tools = self.tool_registry.get_all_tools()
                if tools:
                    lines = [f"  - {t['name']}: {t['description'][:60]}" for t in tools[:12]]
                    tool_hints = "\n已注册工具:\n" + "\n".join(lines) + "\n"

            classify_prompt = (
                f"消息内容: [{text}]\n"
                f"{tool_hints}\n"
                "请判断该消息是否包含需要执行的动作（如发送图片、搜索信息等）。\n"
                "只输出 JSON，格式：\n"
                '{"action": "generate_image" | null, "param": "动作参数或null", "clean_text": "去掉动作描述后的剩余文本"}\n'
                "如果没有需要执行的动作，action 填 null。"
            )

            kwargs: dict[str, Any] = {
                "system_prompt": "你是消息动作分类器，只输出JSON，不解释。",
                "prompt": classify_prompt,
            }
            if provider_id:
                kwargs["chat_provider_id"] = provider_id

            response = await self.context.llm_generate(**kwargs)
            raw = (getattr(response, "completion_text", None) or "").strip()

            # 清理 markdown 代码块
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()

            parsed = json.loads(raw)
            action_raw = str(parsed.get("action") or "").lower()
            action = self.ACTION_MAP.get(action_raw)
            if not action:
                return None

            param = str(parsed.get("param") or "").strip()
            clean_text = str(parsed.get("clean_text") or text).strip()
            if not param:
                return None

            logger.info(f"[proactive_action] LLM 分类结果: action={action}, param={param[:40]}")
            return {"action": action, "param": param, "clean_text": clean_text, "method": "llm"}

        except Exception as e:
            logger.debug(f"[proactive_action] LLM 意图分类失败（静默降级）: {e}")
            return None
