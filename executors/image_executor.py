"""生图执行器：通过 aiimg_enhanced 插件实例执行文生图。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from astrbot.core.message.components import Image
except ImportError:
    Image = None  # type: ignore


class ImageExecutor:
    """生图执行器。

    查找 astrbot_plugin_aiimg_enhanced 实例并调用其 draw.generate()。
    失败时静默返回 None（调用方只发文字）。
    """

    def __init__(self, tool_registry: Any) -> None:
        self.tool_registry = tool_registry
        self._aiimg: Any | None = None  # 懒加载，避免初始化时插件尚未就绪

    def _get_aiimg(self) -> Any | None:
        if self._aiimg is not None and hasattr(self._aiimg, "draw"):
            return self._aiimg
        inst = self.tool_registry.find_aiimg_instance()
        if inst is not None:
            self._aiimg = inst
        return self._aiimg

    async def execute(self, prompt: str) -> "Image | None":
        """执行生图，返回 Image 组件或 None。"""
        if Image is None:
            logger.warning("[proactive_action] 无法导入 Image 组件")
            return None

        aiimg = self._get_aiimg()
        if aiimg is None:
            logger.debug("[proactive_action] 未找到 aiimg 实例，跳过生图")
            return None

        try:
            logger.info(f"[proactive_action] 生图 prompt: {prompt[:60]}...")
            image_path, provider_tries = await aiimg.draw.generate(prompt)
            used = next((t["pid"] for t in provider_tries if t["ok"]), "?")
            logger.info(f"[proactive_action] 生图成功，服务商: {used}")
            return Image.fromFileSystem(str(image_path))
        except Exception as e:
            logger.warning(f"[proactive_action] 生图失败（静默降级）: {e}")
            return None
