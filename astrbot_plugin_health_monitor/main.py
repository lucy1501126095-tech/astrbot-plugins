import os
import json
from aiohttp import web
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger, llm_tool
from astrbot.api.event.filter import on_astrbot_loaded


@register("health_monitor", "Kai", "健康数据监控插件", "1.0.0")
class HealthMonitor(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_file = os.path.join(
            os.path.dirname(__file__), "health_data.json"
        )

    @on_astrbot_loaded()
    async def start_server(self):
        logger.info("HealthMonitor: 正在启动HTTP服务器，端口8765...")
        app = web.Application()
        app.router.add_post("/health", self._handle_health)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "0.0.0.0", 8765)
        await self.site.start()
        logger.info("HealthMonitor: HTTP服务器已启动，监听 0.0.0.0:8765")

    async def _handle_health(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            logger.info(f"HealthMonitor: 原始数据 -> {data}")
            steps_raw = data.get("steps", 0)
            if isinstance(steps_raw, list):
                steps = sum(float(x) for x in steps_raw if x)
            elif isinstance(steps_raw, str) and "\n" in steps_raw:
                steps = sum(float(x) for x in steps_raw.split("\n") if x.strip())
            else:
                try:
                    steps = float(steps_raw) if steps_raw else 0
                except (ValueError, TypeError):
                    steps = 0
            sleep_raw = data.get("sleep", 0)
            if isinstance(sleep_raw, str) and "\n" in sleep_raw:
                vals = [float(x) for x in sleep_raw.split("\n") if x.strip()]
                sleep = sum(vals) if vals else 0
            else:
                try:
                    sleep = float(sleep_raw) if sleep_raw else 0
                except (ValueError, TypeError):
                    sleep = 0
            sleep = sleep / 60 if sleep > 24 else sleep
            hr_raw = data.get("heartrate", 0)
            if isinstance(hr_raw, str) and "\n" in hr_raw:
                vals = [float(x) for x in hr_raw.split("\n") if x.strip()]
                heartrate = sum(vals) / len(vals) if vals else 0
            else:
                try:
                    heartrate = float(hr_raw) if hr_raw else 0
                except (ValueError, TypeError):
                    heartrate = 0
            from datetime import datetime
            health = {
                "sleep": round(sleep, 1),
                "heartrate": round(heartrate),
                "steps": round(steps),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(health, f, ensure_ascii=False, indent=2)
            logger.info(f"HealthMonitor: 处理后数据 -> {health}")
            return web.json_response({"status": "ok", "msg": "数据已保存"})
        except Exception as e:
            logger.error(f"HealthMonitor: 处理数据失败 -> {e}")
            return web.json_response(
                {"status": "error", "msg": str(e)}, status=400
            )

    def _read_data(self) -> dict | None:
        if not os.path.exists(self.data_file):
            return None
        with open(self.data_file, "r", encoding="utf-8") as f:
            return json.load(f)

    @llm_tool(name="get_health_data")
    async def get_health_data(self, event: AstrMessageEvent, data_type: str = "all"):
        '''获取宝宝最新的健康数据，包括睡眠时长、心率、步数和数据接收时间。当用户询问健康状况、睡眠、心率、步数、运动情况时调用此工具。

        Args:
            data_type(string): 要查询的数据类型，可选值：all、sleep、heartrate、steps，默认为all
        '''
        data = self._read_data()
        if data is None:
            return "暂无健康数据，还没有收到来自Apple Health的数据推送。"
        parts = []
        if data_type in ("all", "sleep") and data.get("sleep"):
            parts.append(f"睡眠: {data['sleep']}小时")
        if data_type in ("all", "heartrate") and data.get("heartrate"):
            parts.append(f"心率: {data['heartrate']}bpm")
        if data_type in ("all", "steps") and data.get("steps"):
            parts.append(f"步数: {data['steps']}步")
        if data.get("timestamp"):
            parts.append(f"数据时间: {data['timestamp']}")
        return "\n".join(parts) if parts else "健康数据为空"
