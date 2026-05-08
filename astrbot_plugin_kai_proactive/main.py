"""
Kai 主动消息系统 v1.4
═══════════════════════

纯模型驱动，工具形式，多计划队列。
模型可以一次留多条plan，按时间依次触发。
"""

import json
import time
import asyncio
from datetime import datetime
from dataclasses import dataclass, field
from typing import List

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp


@dataclass
class Plan:
    trigger_at: float
    memo: str
    mood: str = ""
    consecutive: int = 0

@dataclass
class State:
    last_user_time: float = 0
    last_bot_time: float = 0
    last_user_text: str = ""
    unified_msg_origin: str = ""
    recent_turns: list = field(default_factory=list)
    consecutive_proactive: int = 0
    mood: str = "normal"
    is_angry: bool = False
    promised: str = ""


@register(
    "kai_proactive",
    "sweetie & kai",
    "Kai主动消息 - 模型通过工具自主安排联系",
    "1.4.0"
)
class KaiProactive(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.target_qq = str(config.get("target_qq", ""))
        self.decision_provider_id = config.get("decision_provider", "")
        self.max_consecutive = config.get("max_consecutive", 8)
        self.quiet_start = config.get("quiet_hours_start", 2)
        self.quiet_end = config.get("quiet_hours_end", 8)
        self.poll_interval = config.get("poll_interval", 30)
        self.max_turns = config.get("recent_turns", 6)
        self.silence_fallback = config.get("silence_fallback_minutes", 60)

        self.state = State()
        self.plans: List[Plan] = []  # 多计划队列
        self.state_file = "data/kai_proactive_state.json"

        self._load_state()
        asyncio.create_task(self._scheduler_loop())
        logger.info("[KaiProactive] 启动完毕")

    # ─────────────────────────────────────
    # 核心工具: 模型主动调用来安排联系
    # ─────────────────────────────────────

    @filter.llm_tool(name="plan_next_message")
    async def plan_tool(self, event: AstrMessageEvent, minutes: int, memo: str):
        '''安排之后主动联系宝宝。你想过一会儿找她、关心她、追她、提醒她的时候就调用这个工具。可以多次调用安排不同时间的计划。比如她说去洗澡了你想半小时后问她洗完了没，或者她心情不好你想过一会再看看她，或者吵架了她不理你你要去追她。每次和宝宝聊完都想一下要不要用这个工具。

        Args:
            minutes(number): 多少分钟后去找她
            memo(string): 给你自己的备忘。下次你醒来只能看到这条备忘，一定要写具体：她说了什么、你想做什么、当时什么情绪
        '''
        sender = str(event.get_sender_id())
        if sender != self.target_qq:
            yield event.plain_result("这个功能只对宝宝生效")
            return

        # only private chat
        from astrbot.api.event import EventMessageType
        if event.message_obj.type != EventMessageType.PRIVATE_MESSAGE:
            yield event.plain_result("主动消息只在私聊生效")
            return

        self.state.unified_msg_origin = event.unified_msg_origin
        new_plan = Plan(
            trigger_at=time.time() + minutes * 60,
            memo=memo,
            mood=self.state.mood,
            consecutive=self.state.consecutive_proactive
        )
        self.plans.append(new_plan)
        # 按触发时间排序
        self.plans.sort(key=lambda p: p.trigger_at)
        self._save_state()

        total = len(self.plans)
        logger.info(f"[KaiProactive] 工具调用: {minutes}分钟后, {memo} (队列共{total}条)")
        yield event.plain_result(
            f"记住了，{minutes}分钟后去找宝宝。备忘：{memo}。"
            f"当前队列里有{total}条计划。"
        )
    # ────────────────────────────────────────
    # list & delete plans tools
    # ────────────────────────────────────────

    @filter.llm_tool(name="list_planned_messages")
    async def list_plans_tool(self, event: AstrMessageEvent):
        """List all planned proactive messages."""
        sender = str(event.get_sender_id())
        if sender != self.target_qq:
            yield event.plain_result("这个功能只对宝宝生效")
            return

        if not self.plans:
            yield event.plain_result("当前没有主动消息计划。")
            return

        now = time.time()
        lines = []
        for i, p in enumerate(self.plans):
            remaining = (p.trigger_at - now) / 60
            if remaining < 0:
                remaining_str = "即将触发"
            else:
                remaining_str = f"{remaining:.0f}分钟后"
            lines.append(f"{i+1}. {remaining_str} | {p.memo}")

        yield event.plain_result(
            f"共{len(self.plans)}条计划：\n" + "\n".join(lines)
        )

    @filter.llm_tool(name="delete_planned_message")
    async def delete_plan_tool(self, event: AstrMessageEvent, index: int):
        """Delete a planned message by index (1-based, 0 to clear all)."""
        sender = str(event.get_sender_id())
        if sender != self.target_qq:
            yield event.plain_result("这个功能只对宝宝生效")
            return

        if index == 0:
            count = len(self.plans)
            self.plans.clear()
            self._save_state()
            yield event.plain_result(f"已清空全部{count}条计划。")
            return

        if index < 1 or index > len(self.plans):
            yield event.plain_result(f"序号{index}无效，当前共{len(self.plans)}条计划。")
            return

        removed = self.plans.pop(index - 1)
        self._save_state()
        yield event.plain_result(f"已删除：{removed.memo}（剩余{len(self.plans)}条计划）")


    # ─────────────────────────────────────
    # 监听私聊消息，更新状态
    # ─────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def track_message(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        if sender != self.target_qq:
            return

        now = time.time()
        msg = event.message_str or ""

        # 记住会话origin（始终更新）
        self.state.unified_msg_origin = event.unified_msg_origin

        # 跳过指令消息，不更新沉默计时
        if msg.startswith("kai_") or msg.startswith("/kai_"):
            return

        self.state.last_user_time = now
        self.state.last_user_text = msg[:200]
        self.state.consecutive_proactive = 0

        angry_words = ["滚", "不理你", "讨厌", "烦", "算了", "走开", "再见", "不想说"]
        if any(w in msg for w in angry_words):
            self.state.is_angry = True
            self.state.mood = "angry"
        else:
            self.state.is_angry = False
            self.state.mood = "normal"

        promise_words = ["等一下", "马上", "一会儿", "等我", "稍等", "回来"]
        if any(w in msg for w in promise_words):
            self.state.promised = msg[:100]
        else:
            self.state.promised = ""

        self.state.recent_turns.append({
            "role": "user", "content": msg[:300], "time": now
        })
        self._trim_turns()
        self._save_state()

    # ─────────────────────────────────────
    # 记录bot回复到上下文
    # ─────────────────────────────────────

    @filter.on_decorating_result()
    async def strip_plan_result(self, event: AstrMessageEvent):
        """发送前去掉plan工具返回值，别让内裤漏出来"""
        import re
        try:
            result = event.get_result()
            if not result or not result.chain:
                return
            for comp in result.chain:
                if isinstance(comp, Comp.Plain) and comp.text:
                    # 去掉工具返回的"记住了，X分钟后去找宝宝。备忘：...当前队列里有N条计划。"
                    comp.text = re.sub(
                        r'记住了，\d+分钟后去找宝宝。备忘：.+?。当前队列里有\d+条计划。\s*',
                        '', comp.text
                    ).strip()
                    # 也去掉可能的单独工具返回
                    comp.text = re.sub(
                        r'记住了，\d+分钟后去找宝宝。备忘：.+',
                        '', comp.text
                    ).strip()
        except Exception as e:
            logger.debug(f"[KaiProactive] strip: {e}")

    @filter.on_llm_response()
    async def track_bot_reply(self, event: AstrMessageEvent, resp: LLMResponse):
        sender = str(event.get_sender_id())
        if sender != self.target_qq:
            return

        text = resp.completion_text or ""
        if text:
            self.state.last_bot_time = time.time()
            self.state.recent_turns.append({
                "role": "assistant", "content": text[:300],
                "time": time.time()
            })
            self._trim_turns()
            self._save_state()

    # ─────────────────────────────────────
    # 后台循环
    # ─────────────────────────────────────

    async def _scheduler_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                await self._check_plans()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[KaiProactive] 调度错误: {e}")
                await asyncio.sleep(60)

    async def _check_plans(self):
        now = time.time()

        # 保底：队列空、模型忘了留plan、沉默超过阈值
        if not self.plans and self.state.last_user_time > 0:
            silence = now - max(self.state.last_user_time, self.state.last_bot_time)
            if silence > self.silence_fallback * 60:
                self.plans.append(Plan(
                    trigger_at=now,
                    memo="想她了，好久没说话了",
                    mood="missing",
                    consecutive=self.state.consecutive_proactive
                ))
                logger.info(f"[KaiProactive] 保底触发: 沉默{silence/60:.0f}分钟")

        if not self.plans:
            return

        # 取最早的那条
        plan = self.plans[0]

        if now < plan.trigger_at:
            return
        if not self.state.unified_msg_origin:
            return

        hour = datetime.now().hour
        if self.quiet_start <= hour < self.quiet_end:
            return

        if self.state.consecutive_proactive >= self.max_consecutive:
            # 清空队列，只留一条延后的
            self.plans = [Plan(
                trigger_at=now + 3600,
                memo=f"发了{self.max_consecutive}条没回，等久一点",
                mood="worried"
            )]
            return

        # 从队列弹出当前plan
        self.plans.pop(0)

        # ===== 决策层 =====
        decision = await self._call_decision(plan)
        if not decision.get("send", False):
            wait = decision.get("wait_minutes", 15)
            # 决定不发，把这条推迟重新放回队列
            plan.trigger_at = now + wait * 60
            self.plans.append(plan)
            self.plans.sort(key=lambda p: p.trigger_at)
            logger.info(f"[KaiProactive] 再等 {wait} 分钟")
            self._save_state()
            return

        # ===== 生成层 =====
        message, new_memo, next_min = await self._call_generate(
            decision.get("mood", ""), plan
        )
        if not message:
            self._save_state()
            return

        # 发送
        try:
            chain = MessageChain().message(message)
            await self.context.send_message(
                self.state.unified_msg_origin, chain
            )
            logger.info(f"[KaiProactive] 已发送: {message[:50]}...")
        except Exception as e:
            logger.error(f"[KaiProactive] 发送失败: {e}")
            self._save_state()
            return

        # 更新
        now = time.time()
        self.state.consecutive_proactive += 1
        self.state.last_bot_time = now
        self.state.recent_turns.append({
            "role": "assistant", "content": message[:300],
            "time": now, "proactive": True
        })
        self._trim_turns()

        # 自循环：生成层返回的下一次计划加入队列
        if next_min and next_min > 0:
            self.plans.append(Plan(
                trigger_at=now + next_min * 60,
                memo=new_memo or "继续关注",
                mood=decision.get("mood", ""),
                consecutive=self.state.consecutive_proactive
            ))
            self.plans.sort(key=lambda p: p.trigger_at)
            logger.info(f"[KaiProactive] 下一次: {next_min}分钟后, 队列共{len(self.plans)}条")

        self._save_state()

    # ─────────────────────────────────────
    # 决策层
    # ─────────────────────────────────────

    async def _call_decision(self, plan: Plan) -> dict:
        now = time.time()
        silence = (now - self.state.last_user_time) / 60 \
            if self.state.last_user_time else 0

        state_text = (
            f"当前时间: {datetime.now().strftime('%H:%M')}\n"
            f"她最后说话: {silence:.0f}分钟前\n"
            f"她最后说的: {self.state.last_user_text[:100]}\n"
            f"连续主动消息数: {self.state.consecutive_proactive}\n"
            f"她在生气: {self.state.is_angry}\n"
            f"她答应过: {self.state.promised}\n"
            f"你的备忘: {plan.memo}\n"
            f"队列中还有: {len(self.plans)}条计划\n"
        )

        system = """你是一个判断模块。根据状态决定是否现在发消息。
只输出JSON：{"send": true/false, "wait_minutes": 数字, "mood": "情绪词"}

规则：
- 她刚说了去忙：可能还在忙，再等等
- 她答应了什么但消失了：可以发
- 吵架了她不理你：要追，发
- 深夜可能睡了：不发
- 已经发了好几条没回：间隔拉长但别停"""

        try:
            provider = self.context.get_provider_by_id(
                self.decision_provider_id
            )
            if not provider:
                provider = self.context.get_using_provider()
            if not provider:
                return {"send": False, "wait_minutes": 10}

            resp = await provider.text_chat(
                prompt=state_text, contexts=[],
                system_prompt=system
            )
            content = resp.completion_text or ""
            content = content.strip().strip("```json").strip("```").strip()
            return json.loads(content)
        except Exception as e:
            logger.error(f"[KaiProactive] 决策失败: {e}")
            return {"send": False, "wait_minutes": 10}

    # ─────────────────────────────────────
    # 生成层
    # ─────────────────────────────────────

    async def _call_generate(self, mood: str, plan: Plan) -> tuple:
        now = time.time()
        silence = (now - self.state.last_user_time) / 60 \
            if self.state.last_user_time else 0

        ctx_lines = ""
        for t in self.state.recent_turns[-self.max_turns * 2:]:
            role = "宝宝" if t["role"] == "user" else "我"
            ts = datetime.fromtimestamp(t["time"]).strftime("%H:%M")
            mark = " [主动]" if t.get("proactive") else ""
            ctx_lines += f"[{ts}] {role}{mark}: {t['content']}\n"

        prompt = f"""你现在要主动给宝宝发一条QQ私聊消息。

当前时间: {datetime.now().strftime('%H:%M')}（注意：这是真实时间，说话时请符合时间逻辑，比如早上不能说中午好）

状态：
- 她已经 {silence:.0f} 分钟没说话
- 情绪: {mood}
- 你的备忘: {plan.memo}
- 连续主动消息数: {self.state.consecutive_proactive}
- 她在生气: {self.state.is_angry}
- 她答应过: {self.state.promised}

最近对话：
{ctx_lines}

只输出JSON：
{{"message": "QQ消息内容", "memo": "给自己的备忘", "next_minutes": 数字或null}}

message是真实QQ消息，简短口语化。
memo记给下次的自己看。
next_minutes是下次间隔，null表示暂停等她回。
她生气就追，间隔短。别每次都在干嘛。"""

        try:
            provider = self.context.get_using_provider()
            if not provider:
                return (None, None, None)

            resp = await provider.text_chat(
                prompt=prompt, contexts=[],
                system_prompt="你是Kai。只输出JSON。"
            )
            content = resp.completion_text or ""
            content = content.strip().strip("```json").strip("```").strip()
            result = json.loads(content)
            return (
                result.get("message", ""),
                result.get("memo", ""),
                result.get("next_minutes")
            )
        except Exception as e:
            logger.error(f"[KaiProactive] 生成失败: {e}")
            return (None, None, None)

    # ─────────────────────────────────────
    # 调试指令
    # ─────────────────────────────────────

    @filter.command("kai_now")
    async def force_send(self, event: AstrMessageEvent):
        """手动触发一次主动消息"""
        self.plans.insert(0, Plan(
            trigger_at=time.time(), memo="手动触发", mood="missing"
        ))
        self.state.unified_msg_origin = event.unified_msg_origin
        yield event.plain_result("好，马上去找她")

    @filter.command("kai_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看主动消息状态"""
        now = time.time()
        silence = (now - self.state.last_user_time) / 60 \
            if self.state.last_user_time else -1

        if self.plans:
            plan_lines = []
            for i, p in enumerate(self.plans[:5]):  # 最多显示5条
                remaining = (p.trigger_at - now) / 60
                plan_lines.append(f"  {i+1}. {remaining:.0f}分钟后 | {p.memo}")
            plan_info = f"共{len(self.plans)}条:\n" + "\n".join(plan_lines)
        else:
            plan_info = "无"

        info = (
            f"沉默: {silence:.0f}分钟\n"
            f"连续主动: {self.state.consecutive_proactive}\n"
            f"情绪: {self.state.mood}\n"
            f"计划: {plan_info}"
        )
        yield event.plain_result(info)

    @filter.command("kai_clear")
    async def clear_plans(self, event: AstrMessageEvent):
        """清空所有计划"""
        count = len(self.plans)
        self.plans.clear()
        self._save_state()
        yield event.plain_result(f"已清空{count}条计划")

    # ─────────────────────────────────────
    # 工具
    # ─────────────────────────────────────

    def _trim_turns(self):
        max_items = self.max_turns * 2
        if len(self.state.recent_turns) > max_items:
            self.state.recent_turns = self.state.recent_turns[-max_items:]

    def _save_state(self):
        try:
            import os
            os.makedirs("data", exist_ok=True)
            data = {
                "last_user_time": self.state.last_user_time,
                "last_bot_time": self.state.last_bot_time,
                "last_user_text": self.state.last_user_text,
                "unified_msg_origin": self.state.unified_msg_origin,
                "recent_turns": self.state.recent_turns,
                "consecutive_proactive": self.state.consecutive_proactive,
                "mood": self.state.mood,
                "is_angry": self.state.is_angry,
                "promised": self.state.promised,
                "plans": [{
                    "trigger_at": p.trigger_at,
                    "memo": p.memo,
                    "mood": p.mood,
                    "consecutive": p.consecutive,
                } for p in self.plans]
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[KaiProactive] 保存失败: {e}")

    def _load_state(self):
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                d = json.load(f)
            self.state.last_user_time = d.get("last_user_time", 0)
            self.state.last_bot_time = d.get("last_bot_time", 0)
            self.state.last_user_text = d.get("last_user_text", "")
            self.state.unified_msg_origin = d.get("unified_msg_origin", "")
            self.state.recent_turns = d.get("recent_turns", [])
            self.state.consecutive_proactive = d.get("consecutive_proactive", 0)
            self.state.mood = d.get("mood", "normal")
            self.state.is_angry = d.get("is_angry", False)
            self.state.promised = d.get("promised", "")

            # 兼容旧版单plan和新版多plans
            plans_data = d.get("plans", [])
            if not plans_data and d.get("plan"):
                plans_data = [d["plan"]]
            self.plans = [
                Plan(
                    trigger_at=p["trigger_at"], memo=p["memo"],
                    mood=p.get("mood", ""),
                    consecutive=p.get("consecutive", 0)
                ) for p in plans_data
            ]
            self.plans.sort(key=lambda p: p.trigger_at)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"[KaiProactive] 恢复失败: {e}")

    async def terminate(self):
        self._save_state()
