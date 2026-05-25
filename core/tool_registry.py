"""工具注册表：扫描 AstrBot 全部已注册 LLM 工具，查找插件实例。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger


class ToolRegistry:
    """运行时工具注册表。

    - 扫描 astrbot.core.provider.register.llm_tools 获取所有已注册工具的名称和描述
    - 通过 context.star_manager 查找特定插件实例（多版本 AstrBot 兼容）
    """

    def __init__(self, context: Any) -> None:
        self.context = context
        self._cached_tools: list[dict] | None = None

    # ── 工具列表 ──────────────────────────────────────────────────────────

    def get_all_tools(self) -> list[dict]:
        """返回全部已注册 LLM 工具的简要信息列表（含 name, description）。"""
        if self._cached_tools is not None:
            return self._cached_tools

        tools: list[dict] = []
        try:
            from astrbot.core.provider.register import llm_tools  # type: ignore

            for t in llm_tools.func_list:
                name = getattr(t, "name", None) or ""
                desc = getattr(t, "description", None) or ""
                if name:
                    tools.append({"name": name, "description": desc})
        except Exception as e:
            logger.debug(f"[proactive_action] 获取 llm_tools 失败: {e}")

        self._cached_tools = tools
        return tools

    def invalidate_cache(self) -> None:
        """清除工具列表缓存（插件热更新后调用）。"""
        self._cached_tools = None

    def get_image_tools(self) -> list[str]:
        """返回所有与生图相关的工具名称。"""
        return [
            t["name"]
            for t in self.get_all_tools()
            if any(
                kw in t["name"].lower() or kw in t["description"].lower()
                for kw in ("image", "draw", "aiimg", "生图", "绘图", "画图")
            )
        ]

    # ── 插件实例查找 ──────────────────────────────────────────────────────

    def find_star_instance(self, plugin_name: str) -> Any | None:
        """通过插件包名查找已加载的 Star 实例。

        兼容策略（从高到低优先级）：
        1. context.star_manager.stars (新版 AstrBot dict)
        2. context.star_manager.loaded_stars (旧版 AstrBot list)
        3. context._star_manager（私有属性兜底）
        """
        star_manager = (
            getattr(self.context, "star_manager", None)
            or getattr(self.context, "_star_manager", None)
        )
        if star_manager is None:
            logger.debug("[proactive_action] 未找到 star_manager")
            return None

        # 新版：stars 是 dict，key 为插件包名或 star 名
        stars_dict = getattr(star_manager, "stars", None)
        if isinstance(stars_dict, dict):
            for key, val in stars_dict.items():
                if plugin_name in str(key):
                    inst = getattr(val, "star_cls", None) or val
                    if inst is not None:
                        logger.debug(f"[proactive_action] 通过 stars dict 找到 {plugin_name}")
                        return inst

        # 旧版：loaded_stars 是 list[StarMetadata]
        loaded = getattr(star_manager, "loaded_stars", None) or []
        for meta in loaded:
            inst = getattr(meta, "star_cls", None) or meta
            module = str(getattr(inst, "__module__", "") or "")
            cls_name = str(getattr(type(inst), "__name__", "") or "")
            if plugin_name in module or plugin_name in cls_name:
                logger.debug(f"[proactive_action] 通过 loaded_stars 找到 {plugin_name}")
                return inst

        logger.debug(f"[proactive_action] 未找到插件实例: {plugin_name}")
        return None

    def find_aiimg_instance(self) -> Any | None:
        """查找 astrbot_plugin_aiimg_enhanced 实例（含 .draw 属性验证）。"""
        inst = self.find_star_instance("astrbot_plugin_aiimg_enhanced")
        if inst is not None and hasattr(inst, "draw"):
            return inst
        return None
