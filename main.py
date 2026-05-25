"""astrbot_plugin_proactive_action — 主动动作附属插件。

通过 OnDecoratingResultEvent 钩子拦截所有出站消息（含主动回复插件发出的主动消息），
识别文本中内嵌的动作指令（如「（发来一张XXX）」），自动调用对应工具执行并将结果
注入消息链，实现多模态对话体验。
"""

from __future__ import annotations

import asyncio

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageEventResult

from .core.classifier import IntentClassifier
from .core.dispatcher import ActionDispatcher
from .core.tool_registry import ToolRegistry
from .executors.image_executor import ImageExecutor

_HANDLER_FULL_NAME = "astrbot_plugin_proactive_action.main_on_decorating_result"
_MODULE_PATH = "astrbot_plugin_proactive_action.main"


class ProactiveActionPlugin(star.Star):
    """主动动作附属插件。"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config: AstrBotConfig = config
        self._plugin_config: dict = {}
        self.tool_registry: ToolRegistry | None = None
        self.classifier: IntentClassifier | None = None
        self.image_executor: ImageExecutor | None = None
        self.dispatcher: ActionDispatcher | None = None
        logger.info("[proactive_action] 插件实例已创建。")

    async def initialize(self) -> None:
        cfg = self.config
        if not hasattr(cfg, 'get'):
            cfg = {}
        self._plugin_config = cfg

        self.tool_registry = ToolRegistry(self.context)
        self.classifier = IntentClassifier(self.context, self._plugin_config, self.tool_registry)
        self.image_executor = ImageExecutor(self.tool_registry)
        self.dispatcher = ActionDispatcher(self._plugin_config, self.classifier, self.image_executor)

        # 向 star_handlers_registry 手动注册 OnDecoratingResultEvent handler
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                StarHandlerMetadata,
                star_handlers_registry,
            )
            from astrbot.core.star.star import star_map, StarMetadata

            # 确保本插件在 star_map 中有记录，否则 only_activated 检查会过滤掉 handler
            if _MODULE_PATH not in star_map:
                meta = StarMetadata(
                    name="astrbot_plugin_proactive_action",
                    author="Justice-ocr",
                    desc="主动动作附属插件",
                    version="1.0.0",
                    repo="",
                    star_cls_type=type(self),
                    reserved=False,
                )
                meta.activated = True
                star_map[_MODULE_PATH] = meta
            else:
                star_map[_MODULE_PATH].activated = True

            # 去重检查
            existing = star_handlers_registry.get_handlers_by_event_type(
                EventType.OnDecoratingResultEvent,
                only_activated=False,
            )
            already = any(h.handler_full_name == _HANDLER_FULL_NAME for h in existing)

            if not already:
                plugin_self = self

                async def _handler(event: AstrMessageEvent) -> None:
                    await plugin_self._on_decorating_result_impl(event)

                metadata = StarHandlerMetadata(
                    event_type=EventType.OnDecoratingResultEvent,
                    handler_full_name=_HANDLER_FULL_NAME,
                    handler_name="on_decorating_result",
                    handler_module_path=_MODULE_PATH,
                    handler=_handler,
                    event_filters=[],
                    desc="proactive_action: 拦截出站消息并执行动作指令",
                )
                star_handlers_registry.append(metadata)
                logger.info("[proactive_action] OnDecoratingResultEvent handler 注册成功。")
            else:
                logger.info("[proactive_action] OnDecoratingResultEvent handler 已存在，跳过注册。")

        except Exception as e:
            logger.warning(f"[proactive_action] 注册 OnDecoratingResultEvent handler 失败: {e}")

        logger.info("[proactive_action] 初始化完成。")

    async def terminate(self) -> None:
        # 卸载时清理注册的 handler
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )
            existing = star_handlers_registry.get_handlers_by_event_type(
                EventType.OnDecoratingResultEvent,
                only_activated=False,
            )
            for h in existing:
                if h.handler_full_name == _HANDLER_FULL_NAME:
                    star_handlers_registry.remove(h)
                    logger.info("[proactive_action] OnDecoratingResultEvent handler 已清理。")
                    break
        except Exception as e:
            logger.debug(f"[proactive_action] 清理 handler 失败（忽略）: {e}")
        logger.info("[proactive_action] 插件已卸载。")

    async def _on_decorating_result_impl(self, event: AstrMessageEvent) -> None:
        if not self._plugin_config.get("enable", True):
            return
        if self.dispatcher is None:
            return
        res: MessageEventResult | None = event.get_result()
        if res is None or not res.chain:
            return
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            new_chain = await self.dispatcher.process_chain(
                res.chain, session_id=session_id, source="bot_outgoing"
            )
            if new_chain is not res.chain:
                res.chain = new_chain
                event.set_result(res)
        except Exception as e:
            logger.error(f"[proactive_action] on_decorating_result 异常（已忽略）: {e}")

    @filter.command("pa_tools", alias={"proactive_tools", "动作工具列表"})
    async def cmd_list_tools(self, event: AstrMessageEvent) -> None:
        """列出当前 AstrBot 已注册的全部 LLM 工具。"""
        if self.tool_registry is None:
            yield event.plain_result("插件尚未初始化，请稍后再试。")
            return

        self.tool_registry.invalidate_cache()
        tools = self.tool_registry.get_all_tools()

        if not tools:
            yield event.plain_result("⚠️ 未找到任何已注册的 LLM 工具。")
            return

        lines = [f"🔧 已注册 LLM 工具（共 {len(tools)} 个）\n━━━━━━━━━━━━━━"]
        for i, t in enumerate(tools, 1):
            name = t.get("name", "?")
            desc = t.get("description", "").strip()
            if len(desc) > 60:
                desc = desc[:57] + "..."
            lines.append(f"{i}. {name}\n   {desc}" if desc else f"{i}. {name}")

        yield event.plain_result("\n".join(lines))
        event.stop_event()
        event.should_call_llm(True)

    async def on_incoming_message(self, event: AstrMessageEvent) -> None:
        """扫描用户发来的消息（需配置 scan_incoming=true）。"""
        if not self._plugin_config.get("incoming_config", {}).get("scan_incoming", False):
            return
        if self.dispatcher is None or self.classifier is None:
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        intent = await self.classifier.classify(text, source="incoming")
        if not intent or intent.get("action") != "image" or not intent.get("param"):
            return
        asyncio.create_task(self._send_generated_image(event, intent["param"]))

    async def _send_generated_image(self, event: AstrMessageEvent, prompt: str) -> None:
        if self.image_executor is None:
            return
        try:
            img = await self.image_executor.execute(prompt)
            if img is not None:
                await event.send(event.chain_result([img]))
        except Exception as e:
            logger.warning(f"[proactive_action] 后台生图发送失败: {e}")
