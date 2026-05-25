"""动作调度器：协调意图分类和工具执行，处理消息链的修改。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

try:
    from astrbot.core.message.components import Plain
except ImportError:
    Plain = None  # type: ignore


class ActionDispatcher:
    """动作调度器。

    从消息链中提取文本 → 意图分类 → 执行工具 → 修改消息链。
    所有失败均静默降级，保证原消息链不受影响。
    """

    def __init__(
        self,
        config: dict,
        classifier: Any,
        image_executor: Any,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.image_executor = image_executor

    # ── 主入口 ────────────────────────────────────────────────────────────

    async def process_chain(
        self,
        chain: list,
        session_id: str = "",
        source: str = "bot_outgoing",
    ) -> list:
        """处理消息链，返回（可能被修改的）新链。

        如果链里的文本包含动作指令：
        - 将文本中的指令括号删除（用 clean_text 替换原 Plain）
        - 根据 image_generation_mode 在文本前/后插入生成的图片组件

        任何异常都不会改变原链（返回原链）。
        """
        if not chain:
            return chain

        try:
            return await self._process(chain, session_id, source)
        except Exception as e:
            logger.error(f"[proactive_action] 消息链处理异常（原链返回）: {e}")
            return chain

    # ── 内部实现 ──────────────────────────────────────────────────────────

    async def _process(
        self, chain: list, session_id: str, source: str
    ) -> list:
        # 1. 提取所有 Plain 组件的文本（合并）
        text_parts: list[tuple[int, str]] = []  # (index, text)
        for idx, comp in enumerate(chain):
            text = self._get_plain_text(comp)
            if text:
                text_parts.append((idx, text))

        if not text_parts:
            return chain

        # 2. 合并文本做意图分析（通常只有一个 Plain）
        combined_text = " ".join(t for _, t in text_parts)
        intent = await self.classifier.classify(combined_text, source=source)
        if not intent:
            return chain

        action = intent.get("action")
        param = intent.get("param", "")
        clean_text = intent.get("clean_text", combined_text)
        method = intent.get("method", "?")

        logger.info(
            f"[proactive_action] 命中动作: action={action}, method={method}, "
            f"session={session_id[:30] if session_id else '?'}"
        )

        # 3. 执行工具
        extra_component = None
        if action == "image":
            extra_component = await self.image_executor.execute(param)

        # 无论工具是否成功，都用 clean_text 替换原文本（去掉括号指令）
        new_chain = list(chain)
        if text_parts:
            # 只替换第一个 Plain（含有指令的那个）
            first_idx, _ = text_parts[0]
            replaced_plain = self._make_plain(clean_text)
            if replaced_plain is not None:
                new_chain[first_idx] = replaced_plain
            elif not clean_text:
                # clean_text 为空时直接删除该 Plain
                new_chain.pop(first_idx)

        # 4. 注入工具执行结果
        if extra_component is not None:
            mode = self.config.get("image_config", {}).get("image_generation_mode", "before")
            if mode == "before":
                new_chain = [extra_component] + new_chain
            else:
                new_chain = new_chain + [extra_component]

        return new_chain

    # ── 组件工具方法 ──────────────────────────────────────────────────────

    @staticmethod
    def _get_plain_text(comp: Any) -> str:
        """从组件中提取文本，非 Plain 返回空串。"""
        if Plain is None:
            return ""
        if isinstance(comp, Plain):
            return getattr(comp, "text", "") or ""
        # 兼容字典格式的组件
        if isinstance(comp, dict) and comp.get("type") == "text":
            return str(comp.get("text", ""))
        return ""

    @staticmethod
    def _make_plain(text: str) -> Any | None:
        """构造 Plain 组件，文本为空时返回 None。"""
        if not text or Plain is None:
            return None
        return Plain(text=text)

