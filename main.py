"""astrbot_plugin_proactive_action — 主动动作附属插件。

通过 OnDecoratingResultEvent 钩子拦截所有出站消息（含主动回复插件发出的主动消息），
识别文本中内嵌的动作指令（如「（发来一张XXX）」），自动调用对应工具执行并将结果
注入消息链，实现多模态对话体验。

此外提供"扫描用户消息"可选模式，可从用户发来的消息中识别动作并主动执行。

设计原则：
- 不修改任何其他插件的代码
- 所有失败均静默降级，绝不影响原始消息发送
- 支持扩展：在 executors/ 目录下添加新执行器即可接入新工具
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


@star.register(
    name="astrbot_plugin_proactive_action",
    desc="监测所有出站消息，自动识别并执行动作指令（生图等），实现多模态对话体验。",
    version="1.0.0",
    author="Justice-ocr",
)
class ProactiveActionPlugin(star.Star):
    """主动动作附属插件主类。"""

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
        """异步初始化：读取配置，构建组件。"""
        # AstrBotConfig 本身支持 .get()，直接使用；dict() 包装在部分版本会把 config 当字符串迭代
        cfg = self.config
        self._plugin_config = cfg if isinstance(cfg, dict) else (dict(cfg) if hasattr(cfg, 'keys') else {})

        self.tool_registry = ToolRegistry(self.context)
        self.classifier = IntentClassifier(
            self.context, self._plugin_config, self.tool_registry
        )
        self.image_executor = ImageExecutor(self.tool_registry)
        self.dispatcher = ActionDispatcher(
            self._plugin_config, self.classifier, self.image_executor
        )

        # 兼容旧版 AstrBot：若 @filter.on_decorating_result() 不存在，
        # 手动向 star_handlers_registry 注册 handler。
        # 新版 AstrBot 有 filter.on_decorating_result()，handler 方法上的装饰器已完成注册，这里是双保险。
        try:
            from astrbot.core.star.star_handler import EventType, star_handlers_registry

            class _HandlerWrapper:
                handler_full_name = "ProactiveActionPlugin.on_decorating_result"
                handler_module_path = __name__

                def __init__(self, fn):
                    self.handler = fn

            already_registered = any(
                getattr(h, "handler_full_name", "") == _HandlerWrapper.handler_full_name
                for h in star_handlers_registry.get_handlers_by_event_type(
                    EventType.OnDecoratingResultEvent
                )
            )
            if not already_registered:
                star_handlers_registry.add_handler(
                    EventType.OnDecoratingResultEvent,
                    _HandlerWrapper(self._on_decorating_result_impl),
                )
                logger.info("[proactive_action] 已手动注册 OnDecoratingResultEvent handler（兼容模式）。")
        except Exception as e:
            logger.debug(f"[proactive_action] 手动注册 handler 失败（使用装饰器路径）: {e}")

        logger.info(
            "[proactive_action] 初始化完成。"
            f" 生图={self._plugin_config.get('enable_image_generation', True)}"
            f" 图文顺序={self._plugin_config.get('image_generation_mode', 'before')}"
            f" 扫描用户消息={self._plugin_config.get('scan_incoming', False)}"
            f" 动态工具扫描={self._plugin_config.get('tool_scan_enabled', False)}"
        )

    async def terminate(self) -> None:
        logger.info("[proactive_action] 插件已卸载。")

    # ── Hook：出站消息（主路径）──────────────────────────────────────────

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """OnDecoratingResultEvent 钩子：拦截所有出站消息链。

        覆盖路径：
        - 用户消息触发的 LLM 回复（AstrBot 标准流程）
        - 主动回复插件（astrbot_plugin_proactive_chat）发出的主动消息
        - 任何调用了 _trigger_decorating_hooks 的发送路径

        设计：只修改链，不额外发消息；发送由 AstrBot 框架完成。
        """
        await self._on_decorating_result_impl(event)

    async def _on_decorating_result_impl(self, event: AstrMessageEvent) -> None:
        """实际处理逻辑，供装饰器路径和手动注册路径共用。"""
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
                res.chain,
                session_id=session_id,
                source="bot_outgoing",
            )
            if new_chain is not res.chain:
                res.chain = new_chain
                event.set_result(res)
        except Exception as e:
            logger.error(f"[proactive_action] on_decorating_result 异常（已忽略）: {e}")

    # ── 可选：扫描用户来消息 ──────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_incoming_message(self, event: AstrMessageEvent) -> None:
        """可选：扫描用户发来的消息（需配置 scan_incoming=true）。

        若用户消息里包含动作指令，在 LLM 正常响应之外，额外触发工具执行
        并将结果作为独立消息异步发出（不影响正常 LLM 响应流程）。

        示例：用户说"（发来一张你现在在弹钢琴的样子）我在等你"
        → 插件在 LLM 回复旁边独立发出一张生成的图片
        """
        if not self._plugin_config.get("scan_incoming", False):
            return
        if self.dispatcher is None or self.classifier is None:
            return

        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        intent = await self.classifier.classify(text, source="incoming")
        if not intent:
            return

        action = intent.get("action")
        param = intent.get("param", "")
        if action != "image" or not param:
            return

        asyncio.create_task(self._send_generated_image(event, param))

    async def _send_generated_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> None:
        """后台生图并独立发出（scan_incoming 路径专用）。"""
        if self.image_executor is None:
            return
        try:
            img = await self.image_executor.execute(prompt)
            if img is not None:
                await event.send(event.chain_result([img]))
        except Exception as e:
            logger.warning(f"[proactive_action] 后台生图发送失败: {e}")
