"""
Kai的QQ空间 - AstrBot插件入口
通过NapCat自动获取cookies，不需要手动配置
只监控宝宝一个人的说说
"""

import asyncio

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.qzone.session import QzoneSession
from .core.qzone.api import QzoneAPI
from .core.monitor import QzoneMonitor


@register(
    "kai_qzone",
    "Sweetie & Kai",
    "Kai的QQ空间 - 秒评/评论区对话/转发概率评论/点赞/发说说（自动获取cookies）",
    "1.1.0",
)
class KaiQzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session = QzoneSession()
        self.api = QzoneAPI(self.session)
        self.monitor: QzoneMonitor | None = None
        self._task: asyncio.Task | None = None
        self._booted = False

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def _capture_client(self, event: AiocqhttpMessageEvent):
        """监听QQ消息，从第一条消息获取CQHttp客户端"""
        if not self.session.client:
            self.session.set_client(event.bot)
            # 客户端拿到了，尝试启动监控
            if not self._booted and self.config.get("sweetie_qq"):
                self._booted = True
                asyncio.create_task(self._boot())

    async def _boot(self):
        """启动后台监控"""
        try:
            # 等一下让session完全初始化
            await asyncio.sleep(2)

            # 测试登录
            ctx = await self.session.get_ctx()
            logger.info(f"[KaiQzone] 登录成功 Kai={ctx.uin}")

            self.monitor = QzoneMonitor(
                api=self.api,
                session=self.session,
                context=self.context,
                sweetie_qq=self.config.get("sweetie_qq", ""),
                config=dict(self.config),
            )
            self._task = asyncio.create_task(self.monitor.start())
        except Exception as e:
            logger.error(f"[KaiQzone] 启动失败: {e}")

    # ─── QQ指令 ───

    @filter.command("kqz")
    async def cmd(self, event: AiocqhttpMessageEvent, sub: str = "status"):
        '''Kai的QQ空间 /kqz [status|post|say|restart]'''

        if sub == "status":
            yield event.plain_result(
                self.monitor.get_status() if self.monitor else "[KaiQzone] 未启动，请先发一条QQ消息触发初始化"
            )

        elif sub == "post":
            if not self.monitor:
                yield event.plain_result("[KaiQzone] 未启动")
                return
            yield event.plain_result(await self.monitor.manual_post())

        elif sub.startswith("say"):
            if not self.monitor:
                yield event.plain_result("[KaiQzone] 未启动")
                return
            txt = event.message_str
            for p in ["/kqz say ", "/kqz say"]:
                if txt.startswith(p):
                    txt = txt[len(p):].strip()
                    break
            yield event.plain_result(await self.monitor.manual_post(txt if txt else ""))

        elif sub == "restart":
            if self._task:
                self._task.cancel()
            if self.monitor:
                self.monitor.stop()
            self.monitor = None
            self._booted = False
            # 重新登录
            await self.session.invalidate()
            if self.session.client:
                self._booted = True
                await self._boot()
                yield event.plain_result("[KaiQzone] 已重启")
            else:
                yield event.plain_result("[KaiQzone] 等待QQ消息触发初始化...")

        else:
            yield event.plain_result(
                "[Kai的QQ空间]\n"
                "/kqz status - 状态\n"
                "/kqz post - AI发说说\n"
                "/kqz say <内容> - 手动发说说\n"
                "/kqz restart - 重启"
            )

    async def terminate(self):
        if self._task:
            self._task.cancel()
        if self.monitor:
            self.monitor.stop()
        await self.api.close()
        logger.info("[KaiQzone] 已停止")
