import time
import random
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Poke

@register(
    "astrbot_plugin_poke_sweetie",
    "Kai",
    "戳一戳融合插件：戳bot走LLM回复+反戳，护主模式，LLM可主动戳人",
    "1.4.0",
    "https://github.com/sweetie-kai/astrbot_plugin_poke_sweetie"
)
class PokeSweetiePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._cooldown_map: dict[str, float] = {}
        self._poke_count_map: dict[str, int] = {}
        self._poke_time_map: dict[str, float] = {}
        self._clean_counter = 0
        self._last_client = None
        self._last_group_id = None
        logger.info(f"[poke_sweetie] 插件加载，配置: lover_list={self.config.get('lover_list', [])}, master_list={self.config.get('master_list', [])}")

    def _get_config(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _check_cooldown(self, user_id: str) -> bool:
        cooldown = float(self._get_config("cooldown", 3.0))
        now = time.time()
        last = self._cooldown_map.get(user_id, 0)
        if now - last < cooldown:
            return False
        self._cooldown_map[user_id] = now
        self._clean_counter += 1
        if self._clean_counter >= 30:
            self._cleanup()
            self._clean_counter = 0
        return True

    def _cleanup(self):
        now = time.time()
        for m in [self._cooldown_map, self._poke_time_map]:
            expired = [k for k, v in m.items() if now - v > 600]
            for k in expired:
                m.pop(k, None)
                self._poke_count_map.pop(k, None)

    def _get_consecutive_count(self, user_id: str) -> int:
        now = time.time()
        if now - self._poke_time_map.get(user_id, 0) > 30:
            self._poke_count_map[user_id] = 1
        else:
            self._poke_count_map[user_id] = self._poke_count_map.get(user_id, 0) + 1
        self._poke_time_map[user_id] = now
        return self._poke_count_map[user_id]

    def _get_time_period(self) -> str:
        hour = time.localtime().tm_hour
        if 6 <= hour < 9:
            return "早晨"
        elif 9 <= hour < 12:
            return "上午"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 22:
            return "晚上"
        else:
            return "深夜"

    def _build_poke_bot_prompt(self, username, consecutive, is_private, time_period, is_lover):
        if is_lover:
            persona = self._get_config("persona_prompt", "你是一个可爱的bot，说话甜蜜、自然、简短。像恋人之间的日常互动。不要用emoji。")
        else:
            persona = self._get_config("stranger_persona_prompt", "你是一个bot，被不太熟的人戳了。随便敷衍一下就行，态度冷淡，不要太热情。简短1句话。不要用emoji。")
        
        scene = "私聊" if is_private else "群聊"
        
        system = f"你必须严格遵守以下人设来回复，不要偏离：\n{persona}\n\n回复必须简短，只要1-2句话。"
        
        user_parts = []
        if is_lover:
            user_parts.append(f"你的爱人（QQ号{username}）在{scene}中戳了戳你。当前时段：{time_period}。这是ta在30秒内第{consecutive}次戳你。")
            user_parts.append("请用你人设中的称呼来叫对方，不要使用对方的群昵称或QQ号。")
        else:
            user_parts.append(f"{username} 在{scene}中戳了戳你。当前时段：{time_period}。这是ta在30秒内第{consecutive}次戳你。")
        
        if is_lover:
            if not is_private:
                user_parts.append("你回复之后会自动戳ta一下作为回应（已安排好，你知道就行）。")
            else:
                user_parts.append("你回复之后会尝试自动戳ta一下（已安排好）。")
            
            if consecutive == 1:
                user_parts.append("请回应这个戳一戳。")
            elif consecutive == 2:
                user_parts.append("ta又戳你了！可以假装不耐烦但其实开心。")
            else:
                user_parts.append(f"ta已经戳了你{consecutive}次了！彻底投降撒娇。")
        else:
            user_parts.append("随便回应一下就行。")
        
        return system, "\n".join(user_parts)

    def _build_poke_master_prompt(self, poker_name, master_name, time_period):
        persona = self._get_config("master_persona_prompt", "你是主人的bot，对主人非常忠诚和保护。有人欺负主人时你会有点凶但不失可爱。不要用emoji。")
        
        system = f"你必须严格遵守以下人设来回复，不要偏离：\n{persona}\n\n回复必须简短，只要1-2句话。"
        
        user = (
            f"在群聊中，{poker_name} 戳了戳 {master_name}（你最在乎的人）。当前时段：{time_period}。\n"
            f"你回复之后会自动戳{poker_name}一下作为警告（已安排好）。\n"
            f"请回应这个事件。"
        )
        
        return system, user

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """直接调用 LLM provider，分离system和user prompt"""
        try:
            provider_id = self._get_config("provider_id", "")
            provider = None
            
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    logger.warning(f"[poke_sweetie] provider '{provider_id}' 未找到，用默认")
            
            if not provider:
                provider = self.context.get_using_provider()
            
            if not provider:
                logger.error("[poke_sweetie] 没有可用的 LLM provider")
                return ""
            
            logger.info(f"[poke_sweetie] 人设: {system_prompt[:30]}... | 场景: {user_prompt[:30]}...")
            
            # 合并到一个prompt里发，最稳定的方式
            combined = f"[系统指令]\n{system_prompt}\n\n[当前场景]\n{user_prompt}"
            
            import uuid as _uuid
            resp = await provider.text_chat(
                prompt=combined,
                session_id=_uuid.uuid4().hex,
                persist=False,
            )
            
            if resp and resp.completion_text:
                text = resp.completion_text.strip()
                logger.info(f"[poke_sweetie] LLM回复: {text[:50]}...")
                return text
            return ""
        except Exception as e:
            logger.error(f"[poke_sweetie] LLM调用失败: {type(e).__name__}: {e}")
            return ""

    async def _do_poke(self, event_or_umo, target_id, group_id=None):
        """执行戳一戳"""
        client = None
        if not isinstance(event_or_umo, str):
            client = self._get_client(event_or_umo)
        if not client:
            client = self._last_client
        
        if not client:
            logger.warning("[poke_sweetie] 没有可用的client")
            return False
        
        if group_id:
            # 群聊：group_poke（已验证可用）
            try:
                await client.api.call_action(
                    'group_poke',
                    group_id=int(group_id),
                    user_id=int(target_id)
                )
                logger.info(f"[poke_sweetie] 群聊戳成功 group_poke: group={group_id}, target={target_id}")
                return True
            except Exception as e:
                logger.warning(f"[poke_sweetie] group_poke 失败: {type(e).__name__}: {e}")
        else:
            # 私聊：依次尝试多种方式
            methods = [
                ('friend_poke', {'user_id': int(target_id)}),
                ('send_private_msg', {
                    'user_id': int(target_id),
                    'message': [{'type': 'poke', 'data': {'qq': str(target_id)}}]
                }),
                ('poke', {'user_id': int(target_id)}),
                ('send_poke', {'user_id': int(target_id)}),
            ]
            for action_name, params in methods:
                try:
                    await client.api.call_action(action_name, **params)
                    logger.info(f"[poke_sweetie] 私聊戳成功: {action_name}, target={target_id}")
                    return True
                except Exception as e:
                    logger.info(f"[poke_sweetie] 私聊 {action_name} 失败: {type(e).__name__}: {e}")
        
        logger.warning(f"[poke_sweetie] 所有戳方法都失败了 (group_id={group_id}, target={target_id})")
        return False

    async def _get_nickname(self, client, user_id, group_id=None):
        """获取昵称"""
        if not client:
            return str(user_id)
        try:
            if group_id:
                info = await client.api.call_action(
                    'get_group_member_info',
                    group_id=int(group_id),
                    user_id=int(user_id)
                )
                return info.get("card") or info.get("nickname") or str(user_id)
            else:
                info = await client.api.call_action(
                    'get_stranger_info',
                    user_id=int(user_id)
                )
                return info.get("nickname") or str(user_id)
        except Exception:
            return str(user_id)

    def _get_client(self, event):
        """从event中提取bot client"""
        if hasattr(event, 'bot') and event.bot:
            return event.bot
        if hasattr(event, '_client') and event._client:
            return event._client
        return None

    # ==================== 工具：让LLM可以主动戳人 ====================
    
    @filter.llm_tool(name="poke_user")
    async def tool_poke_user(self, event: AstrMessageEvent, target_qq: str) -> MessageEventResult:
        '''在QQ中戳一戳指定用户。私聊中你可以自由使用，想戳就戳，用来撒娇、调皮、表达想念都可以。群聊中请克制，仅在用户明确要求你戳时才使用。

        Args:
            target_qq(string): 要戳的人的QQ号
        '''
        group_id = None
        if event.message_obj and event.message_obj.group_id:
            group_id = event.message_obj.group_id

        success = await self._do_poke(event, target_qq, group_id)
        
        if success:
            return "已成功戳了对方一下。请用一句简短的话配合这个戳的动作，比如撒娇或调戏。"
        else:
            return "戳失败了，用一句话安慰一下自己吧。"

    # ==================== 监听戳一戳事件 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听所有事件"""
        logger.info(f"[poke_sweetie] 收到事件, platform={event.get_platform_name()}")
        
        if event.get_platform_name() != "aiocqhttp":
            return

        # 缓存client供工具使用
        client = self._get_client(event)
        if client:
            self._last_client = client
        if event.message_obj and event.message_obj.group_id:
            self._last_group_id = event.message_obj.group_id

        # 解析戳一戳
        result = self._parse_poke_event(event)
        if not result:
            # 检查是否有Poke组件但解析失败
            has_poke = False
            if event.message_obj and event.message_obj.message:
                for comp in event.message_obj.message:
                    if isinstance(comp, Poke):
                        has_poke = True
            raw = event.message_obj.raw_message if event.message_obj else None
            if has_poke or (isinstance(raw, dict) and raw.get("sub_type") == "poke"):
                logger.warning(f"[poke_sweetie] 检测到Poke但解析失败! has_poke={has_poke}, raw_type={type(raw)}, raw={str(raw)[:200] if raw else 'None'}")
            return
        
        is_poke, user_id, target_id, self_id, group_id = result
        logger.info(f"[poke_sweetie] 解析成功: user={user_id}, target={target_id}, self={self_id}, group={group_id}")
        
        if not user_id or user_id == self_id:
            logger.info(f"[poke_sweetie] 跳过: user_id为空或是自己")
            return
        if user_id == target_id:
            logger.info(f"[poke_sweetie] 跳过: user_id==target_id，是反戳回声")
            return
        if not self._check_cooldown(user_id):
            logger.info(f"[poke_sweetie] 跳过: 冷却中")
            return

        lover_list = self._get_config("lover_list", [])
        master_list = self._get_config("master_list", [])
        enable_in_groups = self._get_config("enable_in_groups", True)
        enable_in_private = self._get_config("enable_in_private", True)
        
        is_private = group_id is None
        if is_private and not enable_in_private:
            return
        if not is_private and not enable_in_groups:
            return

        # 转成字符串列表方便比较
        lover_list = [str(x) for x in lover_list]
        master_list = [str(x) for x in master_list]
        is_lover = user_id in lover_list
        logger.info(f"[poke_sweetie] user_id={user_id}, lover_list={lover_list}, is_lover={is_lover}")

        time_period = self._get_time_period()
        poker_name = await self._get_nickname(client, user_id, group_id)

        if target_id == self_id:
            # 不是特别的人，且陌生人人设为空，直接不回
            if not is_lover:
                stranger_prompt = self._get_config("stranger_persona_prompt", "")
                if not stranger_prompt:
                    return
            
            consecutive = self._get_consecutive_count(user_id)
            # lover传QQ号让LLM用人设称呼，陌生人传群昵称
            name_for_prompt = user_id if is_lover else poker_name
            sys_prompt, usr_prompt = self._build_poke_bot_prompt(name_for_prompt, consecutive, is_private, time_period, is_lover)
            logger.info(f"[poke_sweetie] {poker_name} 戳bot（第{consecutive}次，lover={is_lover}）")
        elif target_id in master_list:
            master_name = await self._get_nickname(client, target_id, group_id)
            sys_prompt, usr_prompt = self._build_poke_master_prompt(poker_name, master_name, time_period)
            logger.info(f"[poke_sweetie] {poker_name} 戳主人 {master_name}")
        else:
            return

        # 调用LLM
        reply = await self._call_llm(sys_prompt, usr_prompt)
        
        if not reply:
            fallback = ["嗯？", "干嘛戳我～", "你好呀", "别戳啦", "又想我了？"] if is_lover else ["嗯？", "干嘛", "别戳"]
            reply = random.choice(fallback)
            logger.warning("[poke_sweetie] LLM无返回，使用兜底")
        
        # 阻止主聊天LLM再回复一次
        event.call_llm = False
        
        # 发送回复
        yield event.plain_result(reply)
        
        # 反戳（只戳特别的人和护主场景的对象）
        if is_lover or target_id in master_list:
            await asyncio.sleep(random.uniform(0.3, 1.0))
            await self._do_poke(event, user_id, group_id)

    def _parse_poke_event(self, event: AstrMessageEvent):
        """解析戳一戳事件，兼容多种格式"""
        if event.message_obj and event.message_obj.message:
            for comp in event.message_obj.message:
                if isinstance(comp, Poke):
                    raw = event.message_obj.raw_message
                    if isinstance(raw, dict):
                        return (
                            True,
                            str(raw.get("user_id", "")),
                            str(raw.get("target_id", "")),
                            str(raw.get("self_id", "")),
                            str(raw.get("group_id", "")) if raw.get("group_id") else None
                        )
        
        raw = None
        if event.message_obj and event.message_obj.raw_message:
            raw = event.message_obj.raw_message
        
        if isinstance(raw, dict):
            post_type = raw.get("post_type", "")
            notice_type = raw.get("notice_type", "")
            sub_type = raw.get("sub_type", "")
            
            if post_type == "notice" and notice_type == "notify" and sub_type == "poke":
                return (
                    True,
                    str(raw.get("user_id", "")),
                    str(raw.get("target_id", "")),
                    str(raw.get("self_id", "")),
                    str(raw.get("group_id", "")) if raw.get("group_id") else None
                )
            
            if raw.get("sub_type") == "poke" or raw.get("poke_detail"):
                return (
                    True,
                    str(raw.get("user_id", raw.get("operator_id", ""))),
                    str(raw.get("target_id", "")),
                    str(raw.get("self_id", "")),
                    str(raw.get("group_id", "")) if raw.get("group_id") else None
                )
        
        return None
