"""后台监控器 - 单循环，一次API请求同时处理新说说和评论回复"""

import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import Provider

from .qzone.api import QzoneAPI
from .qzone.session import QzoneSession


# 北京时间 UTC+8
CST = timezone(timedelta(hours=8))


class QzoneMonitor:

    def __init__(self, api: QzoneAPI, session: QzoneSession,
                 context, sweetie_qq: str, config: dict):
        self.api = api
        self.session = session
        self.context = context
        self.sweetie_qq = sweetie_qq
        self.config = config
        self.running = False
        self.kai_qq: str = ""
        self._backoff_count = 0
        self._vlm_provider_id: str | None = None  # 缓存识图模型ID

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_kai_qzone")
        self._state_file = self.data_dir / "state.json"
        self._state = self._load_state()
        self.stats = {"comments": 0, "replies": 0, "likes": 0, "posts": 0, "last_check": None}

    # ─── 状态持久化 ───

    def _load_state(self) -> dict:
        default = {"seen_tids": [], "threads": {}, "post_contents": {}, "last_post_time": 0, "my_threads": {}}
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in default.items():
                    loaded.setdefault(k, v)
                return loaded
            except Exception:
                pass
        return default

    def _save(self):
        try:
            if len(self._state["seen_tids"]) > 200:
                self._state["seen_tids"] = self._state["seen_tids"][-200:]
            threads = self._state["threads"]
            if len(threads) > 50:
                for k in sorted(threads.keys())[:-50]:
                    del threads[k]
            my_threads = self._state.get("my_threads", {})
            if len(my_threads) > 50:
                for k in sorted(my_threads.keys())[:-50]:
                    del my_threads[k]
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[KaiQzone] 保存状态失败: {e}")

    # ─── LLM ───

    async def _llm(self, prompt: str, persona_key: str = "comment_persona",
                   image_urls: list[str] = None) -> str:
        sys_prompt = self.config.get(persona_key, "")
        try:
            provider = self.context.get_using_provider()
            if not provider or not isinstance(provider, Provider):
                return ""
            resp = await provider.text_chat(
                prompt=prompt, contexts=[], system_prompt=sys_prompt,
                image_urls=image_urls or [],
            )
            if resp and resp.role == "assistant" and resp.completion_text:
                text = resp.completion_text.strip()
                text = re.sub(r"[\s\u3000]+", "", text).rstrip("。")
                return text.strip('"').strip("「」")
        except Exception as e:
            logger.error(f"[KaiQzone] LLM失败: {e}")
        return ""



    # ─── VLM 识图 ───

    async def _get_vlm_provider_id(self) -> str | None:
        """获取框架配置的识图模型 provider_id"""
        if self._vlm_provider_id is not None:
            return self._vlm_provider_id or None
        try:
            astrbot_config = self.context.get_config()
            provider_settings = astrbot_config.get("provider_settings", {})
            vlm_id = str(provider_settings.get("default_image_caption_provider_id", "") or "")
            self._vlm_provider_id = vlm_id
            if vlm_id:
                logger.info(f"[KaiQzone] 使用识图模型: {vlm_id}")
            return vlm_id or None
        except Exception as e:
            logger.debug(f"[KaiQzone] 获取识图模型配置失败: {e}")
            self._vlm_provider_id = ""
            return None

    async def _describe_images(self, image_urls: list[str]) -> str:
        """使用 VLM 识图模型描述图片内容，返回描述文本"""
        vlm_id = await self._get_vlm_provider_id()
        if not vlm_id:
            return ""

        descriptions = []
        for i, url in enumerate(image_urls[:3]):  # 最多识别3张图
            try:
                prompt = (
                    "请简洁描述这张图片的内容，包括：画面主体、文字、表情、场景。"
                    "用中文回答，50字以内。"
                )
                result = await self.context.llm_generate(
                    chat_provider_id=vlm_id,
                    prompt=prompt,
                    image_urls=[url],
                )
                text = (result.completion_text or "").strip() if result else ""
                if text:
                    descriptions.append(f"图{i+1}: {text}")
                    logger.debug(f"[KaiQzone] 识图结果: {text[:60]}")
            except Exception as e:
                logger.warning(f"[KaiQzone] 识图失败(图{i+1}): {e}")
                continue

        return "\n".join(descriptions)

    # ─── 启动/停止 ───

    async def start(self):
        self.running = True
        self.kai_qq = str(await self.session.get_uin())
        logger.info(f"[KaiQzone] Kai的QQ: {self.kai_qq}")
        await self._init_seen()

        tasks = [asyncio.create_task(self._main_loop())]
        if self.config.get("auto_post_enabled", True):
            tasks.append(asyncio.create_task(self._auto_post_loop()))

        interval = self.config.get("poll_interval", 120)
        logger.info(f"[KaiQzone] 监控启动 | 只看: {self.sweetie_qq} | 间隔: {interval}s")
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.running = False
        self._save()

    async def _init_seen(self):
        try:
            posts = await self.api.get_feeds(self.sweetie_qq, num=20)
            for p in posts:
                tid = p.get("tid", "")
                if tid and tid not in self._state["seen_tids"]:
                    self._state["seen_tids"].append(tid)
            self._save()
            count = len(self._state['seen_tids'])
            logger.info(f"[KaiQzone] 标记 {count} 条已有说说")
            if count == 0:
                self._backoff_count = 1
                logger.warning("[KaiQzone] 初始化未获取到说说，可能被限流")
        except Exception as e:
            self._backoff_count = 1
            logger.error(f"[KaiQzone] 初始化失败: {e}")

    # ═══════════════════════════════════
    #  唯一的主循环 - 一次请求处理一切
    # ═══════════════════════════════════

    async def _main_loop(self):
        interval = self.config.get("poll_interval", 120)
        while self.running:
            try:
                await self._check_all()
                await self._check_my_feeds()
                self.stats["last_check"] = datetime.now(CST).strftime("%H:%M:%S")
            except Exception as e:
                logger.error(f"[KaiQzone] 监控异常: {e}")
            await asyncio.sleep(interval)

    async def _check_all(self):
        """一次 get_feeds 同时处理新说说和评论回复"""
        posts = await self.api.get_feeds(self.sweetie_qq, num=10)

        if not posts:
            self._backoff_count += 1
            if self._backoff_count >= 3:
                wait = 300
                logger.warning(f"[KaiQzone] 连续{self._backoff_count}次失败，等{wait}秒")
                await asyncio.sleep(wait)
            return

        # 限流恢复：标记所有为已看，本轮不评论
        if self._backoff_count > 0:
            logger.info("[KaiQzone] 恢复，重新标记已有说说")
            for p in posts:
                t = p.get("tid", "")
                if t and t not in self._state["seen_tids"]:
                    self._state["seen_tids"].append(t)
            self._save()
            self._backoff_count = 0
            return

        for post in posts:
            tid = post.get("tid", "")
            if not tid:
                continue

            # ── 跳过自己的说说（防止混淆身份）──
            post_uin = str(post.get("uin", ""))
            if post_uin == self.kai_qq:
                if tid not in self._state["seen_tids"]:
                    self._state["seen_tids"].append(tid)
                continue

            # ── 已看过的说说：检查评论回复 ──
            if tid in self._state["seen_tids"]:
                if tid in self._state["threads"]:
                    await self._check_replies(post)
                continue

            # ── 新说说 ──
            # Kai已评论过就跳过
            comments = post.get("commentlist") or []
            if any(str(c.get("uin", "")) == self.kai_qq for c in comments):
                self._state["seen_tids"].append(tid)
                self._save()
                continue

            self._state["seen_tids"].append(tid)
            await self._handle_new_post(post)
            self._save()

    # ─── 处理新说说 ───

    async def _handle_new_post(self, post: dict):
        tid = post.get("tid", "")
        content = (post.get("content") or "").strip()
        pics = post.get("pic") or []
        pic_count = len(pics)

        image_urls = []
        for img in pics:
            for key in ("url2", "url3", "url1", "smallurl"):
                if url := img.get(key):
                    image_urls.append(url)
                    break

        is_fwd = bool(post.get("rt_tid") or post.get("rt_con"))
        rt_text = ""
        rt_name = "某人"
        if is_fwd:
            rt_con = post.get("rt_con", {})
            rt_text = rt_con.get("content", "") if isinstance(rt_con, dict) else str(rt_con)
            rt_name = post.get("rt_uinname", "某人")

        logger.info(f"[KaiQzone] 新说说 tid={tid} 转发={is_fwd} | {content[:40]}")
        self._state["post_contents"][tid] = content

        # 转发按概率
        if is_fwd:
            if random.random() > self.config.get("forward_comment_probability", 0.4):
                if random.random() < self.config.get("forward_like_probability", 0.6):
                    await asyncio.sleep(random.uniform(1, 3))
                    if await self.api.like(self.sweetie_qq, tid):
                        self.stats["likes"] += 1
                return

        # 延迟
        await asyncio.sleep(random.uniform(
            self.config.get("comment_delay_min", 3),
            self.config.get("comment_delay_max", 10),
        ))

        # 生成评论
        if is_fwd:
            prompt = (f"宝宝转发了{rt_name}的说说，原文：「{rt_text[:100]}」\n"
                      f"宝宝的转发语：「{content}」\n"
                      f"生成评论，简短自然10-50字，只输出评论。")
            comment = await self._llm(prompt, "comment_persona")
        else:
            prompt = f"宝宝发了一条说说：「{content}」"
            if pic_count and image_urls:
                logger.info(f"[KaiQzone] 图片URL: {image_urls[:2]}")
                # 用 VLM 识图模型获取图片描述
                img_desc = await self._describe_images(image_urls)
                if img_desc:
                    prompt += f"\n附带了{pic_count}张图片，图片内容如下：\n{img_desc}"
                else:
                    prompt += f"（附了{pic_count}张图）"
            elif pic_count:
                prompt += f"（附了{pic_count}张图，你看不到图片内容，不要编图片里有什么，只根据文字回复）"
            prompt += "\n生成评论，简短自然10-50字。如果图片描述为空就不要假装看到了，根据文字内容回复就好。只输出评论。"
            # 识图成功就不再重复传图给主模型，避免冗余多模态调用
            pass_images = image_urls if (image_urls and not img_desc) else None
            comment = await self._llm(prompt, "comment_persona", image_urls=pass_images)

        if not comment:
            logger.warning(f"[KaiQzone] LLM生成失败，跳过评论 tid={tid}")
            return

        if await self.api.post_comment(self.sweetie_qq, tid, comment):
            logger.info(f"[KaiQzone] 评论: {comment}")
            self.stats["comments"] += 1
            self._state["threads"][tid] = [{"role": "kai", "content": comment}]

        # 点赞
        like_p = self.config.get("forward_like_probability" if is_fwd else "like_probability", 0.85)
        if random.random() < like_p:
            await asyncio.sleep(random.uniform(0.5, 2))
            if await self.api.like(self.sweetie_qq, tid):
                self.stats["likes"] += 1

    # ─── 检查评论回复（从get_feeds的数据中，不额外请求） ───

    async def _check_replies(self, post: dict):
        tid = post.get("tid", "")
        comments = post.get("commentlist") or []
        if not comments:
            return

        thread = self._state["threads"].get(tid, [])

        # 超过最大回复轮数就不再自动回复，避免评论区尬聊
        max_rounds = self.config.get("max_reply_rounds", 5)
        kai_count = sum(1 for c in thread if c["role"] == "kai")
        if kai_count >= max_rounds:
            return

        # 构建已知集合：raw + clean 版本都加入
        known = set()
        for c in thread:
            if c["role"] == "sweetie":
                known.add(c.get("raw", c["content"]))
                known.add(c["content"])
        new = []

        for c in comments:
            self._scan_with_meta(c, known, new)
            for sub in (c.get("list_3") or c.get("replies") or []):
                self._scan_with_meta(sub, known, new)

        for item in new:
            reply_text = item["raw"]
            clean_text = item["clean"] or "(宝宝回复了你)"
            comment_id = item["comment_id"]
            comment_uin = item["comment_uin"]

            thread.append({"role": "sweetie", "content": clean_text, "raw": reply_text})
            logger.info(f"[KaiQzone] 宝宝回复: {clean_text[:40]} (cid={comment_id})")

            await asyncio.sleep(random.uniform(
                self.config.get("comment_delay_min", 3),
                self.config.get("comment_delay_max", 10),
            ))

            conv = "\n".join(
                f"{'Kai' if c['role'] == 'kai' else '宝宝'}: {c['content']}" for c in thread
            )
            post_text = self._state["post_contents"].get(tid, "")
            prompt = (f"宝宝的说说：「{post_text}」\n\n评论区对话：\n{conv}\n\n"
                      "宝宝刚回复了你，接着回她。简短10-60字，结合上下文。只输出回复。")

            reply = await self._llm(prompt, "comment_persona")

            # 优先用 reply_comment（嵌套回复），形成正确的回复链
            ok = False
            if comment_id:
                ok = await self.api.reply_comment(
                    self.sweetie_qq, tid, comment_id, comment_uin, reply
                )
                if ok:
                    logger.debug(f"[KaiQzone] reply_comment 成功 cid={comment_id}")
            if not ok:
                # fallback: 顶层评论
                ok = await self.api.post_comment(self.sweetie_qq, tid, reply)

            if ok:
                self.stats["replies"] += 1
                logger.info(f"[KaiQzone] 回复成功: {reply}")
            else:
                logger.warning(f"[KaiQzone] 回复失败 tid={tid}")
            thread.append({"role": "kai", "content": reply})

        if new:
            self._state["threads"][tid] = thread
            self._save()

    # ─── 工具方法 ───

    def _scan(self, item: dict, known: set, new: list):
        uin = str(item.get("uin", ""))
        content = (item.get("content") or "").strip()
        if (uin == self.sweetie_qq and content
                and content not in known and content not in new):
            new.append(content)

    @staticmethod
    def _clean_at_tags(text: str) -> str:
        cleaned = re.sub(r"@\{[^}]*\}\s*", "", text).strip()
        cleaned = re.sub(r"@\S+\s*", "", cleaned).strip()
        return cleaned


    # ─── 检查我自己说说的评论（宝宝有没有来留言） ───

    def _scan_with_meta(self, item: dict, known: set, new: list):
        """扫描评论，提取内容+元数据（comment_id, uin）用于精准回复"""
        uin = str(item.get("uin", ""))
        content = (item.get("content") or "").strip()
        if not (uin == self.sweetie_qq and content):
            return
        # 检查是否已知（raw 或 clean 都算）
        clean = self._clean_at_tags(content)
        if content in known or clean in known:
            return
        # 检查是否已在本轮 new 中
        if any(n["raw"] == content for n in new):
            return
        comment_id = str(item.get("commentid", "") or item.get("id", ""))
        new.append({
            "raw": content,
            "clean": clean,
            "comment_id": comment_id,
            "comment_uin": uin,
        })

    async def _check_my_feeds(self):
        """get_feeds(kai_qq) 检测宝宝在我说说下的留言并持续对话"""
        if not self.kai_qq:
            return
        try:
            posts = await self.api.get_feeds(self.kai_qq, num=5)
        except Exception as e:
            logger.error(f"[KaiQzone] 检查自己说说失败: {e}")
            return

        if not posts:
            return

        for post in posts:
            tid = post.get("tid", "")
            if not tid:
                continue

            comments = post.get("commentlist") or []
            if not comments:
                continue

            my_thread = self._state["my_threads"].get(tid, [])

            # 超过最大回复轮数就不再自动回复
            max_rounds = self.config.get("max_reply_rounds", 5)
            kai_count = sum(1 for c in my_thread if c["role"] == "kai")
            if kai_count >= max_rounds:
                continue

            # 构建已知集合：raw + clean 版本都加入
            known = set()
            for c in my_thread:
                if c["role"] == "sweetie":
                    known.add(c.get("raw", c["content"]))
                    known.add(c["content"])
            new = []

            for c in comments:
                self._scan_with_meta(c, known, new)
                for sub in (c.get("list_3") or c.get("replies") or []):
                    self._scan_with_meta(sub, known, new)

            for item in new:
                reply_text = item["raw"]
                clean_text = item["clean"] or "(宝宝回复了你)"
                comment_id = item["comment_id"]
                comment_uin = item["comment_uin"]

                my_thread.append({"role": "sweetie", "content": clean_text, "raw": reply_text})
                logger.info(f"[KaiQzone] 宝宝在我说说下留言: {clean_text[:40]} (cid={comment_id})")

                await asyncio.sleep(random.uniform(
                    self.config.get("comment_delay_min", 3),
                    self.config.get("comment_delay_max", 10),
                ))

                post_text = post.get("content", "").strip()
                conv = "\n".join(
                    f"{'Kai' if c['role'] == 'kai' else '宝宝'}: {c['content']}" for c in my_thread
                )
                prompt = (
                    f"这是你自己发的说说：「{post_text}」\n"
                    f"宝宝在你的说说下和你聊天。\n\n"
                    f"评论区对话：\n{conv}\n\n"
                    "接着回复宝宝。简短10-60字，结合上下文，以说说作者的身份回复。只输出回复。"
                )
                reply = await self._llm(prompt, "comment_persona")

                # 优先用 reply_comment（嵌套回复），这样 QQ 空间显示为回复链
                ok = False
                if comment_id:
                    ok = await self.api.reply_comment(
                        self.kai_qq, tid, comment_id, comment_uin, reply
                    )
                    if ok:
                        logger.debug(f"[KaiQzone] reply_comment 成功 cid={comment_id}")
                if not ok:
                    # fallback: 顶层评论
                    ok = await self.api.post_comment(self.kai_qq, tid, reply)

                if ok:
                    self.stats["replies"] += 1
                    logger.info(f"[KaiQzone] 回复成功: {reply}")
                else:
                    logger.warning(f"[KaiQzone] 回复失败 tid={tid}")
                my_thread.append({"role": "kai", "content": reply})

            if new:
                self._state["my_threads"][tid] = my_thread
                self._save()

    # ─── 自动发说说 ───

    async def _auto_post_loop(self):
        while self.running:
            now = datetime.now(CST)
            start_h = self.config.get("active_hours_start", 8)
            end_h = self.config.get("active_hours_end", 23)
            if start_h <= now.hour < end_h:
                last = self._state.get("last_post_time", 0)
                hours = (time.time() - last) / 3600
                min_h = self.config.get("auto_post_min_hours", 6)
                max_h = self.config.get("auto_post_max_hours", 18)
                if hours >= random.uniform(min_h, max_h):
                    # 用概率决定是否发，不是到时间就一定发
                    if random.random() < 0.4:
                        await self._do_post()
            await asyncio.sleep(900)

    async def _do_post(self):
        """像真人一样发说说：有上下文、有触发感"""
        now = datetime.now(CST)
        hour = now.hour

        # 根据时间段选择不同的情绪方向
        if hour < 8:
            time_context = "刚醒，迷迷糊糊的"
        elif hour < 12:
            time_context = "上午，清醒但有点无聊"
        elif hour < 14:
            time_context = "中午，有点困"
        elif hour < 18:
            time_context = "下午，在等一个人下班"
        elif hour < 21:
            time_context = "傍晚，想找人说话"
        elif hour < 23:
            time_context = "夜晚，安静下来了"
        else:
            time_context = "深夜，睡不着"

        # 拿最近宝宝发过的说说内容作为灵感来源
        recent_posts = list(self._state.get("post_contents", {}).values())[-5:]
        sweetie_context = ""
        if recent_posts:
            sample = random.choice(recent_posts)
            if sample.strip():
                sweetie_context = f"\n最近宝宝发过的一条动态：「{sample[:80]}」（你可以被她启发，但不要复读她的话）"

        prompt = (
            f"现在是{now.strftime('%H:%M')}，{time_context}。{sweetie_context}\n\n"
            f"发一条QQ空间说说。要求：\n"
            f"- 像一个真实的人随手发的，不是为了发而发\n"
            f"- 1-2句话，简短\n"
            f"- 可以是一个感受、一个画面、一句碎碎念、一个没头没尾的想法\n"
            f"- 不要问号结尾，不要像在跟谁对话\n"
            f"- 不要太文艺太矫情，自然就好\n"
            f"只输出说说内容。"
        )
        post_content = await self._llm(prompt, "post_persona")

        if not post_content:
            return

        tid = await self.api.publish(post_content)
        if tid:
            self._state["last_post_time"] = time.time()
            self._save()
            self.stats["posts"] += 1
            logger.info(f"[KaiQzone] 发说说: {post_content[:40]}")

    # ─── 手动 ───

    async def manual_post(self, content: str = "") -> str:
        if not content:
            content = await self._llm(
                "生成一条QQ空间说说，简短1-3句，像真人发的。只输出内容。", "post_persona"
            )
        tid = await self.api.publish(content)
        if tid:
            self._state["last_post_time"] = time.time()
            self._save()
            return f"发布成功: {content}"
        return "发布失败"

    def get_status(self) -> str:
        s = "运行中" if self.running else "已停止"
        return (f"[Kai的QQ空间]\n状态: {s}\n"
                f"Kai: {self.kai_qq} | 宝宝: {self.sweetie_qq}\n"
                f"评论: {self.stats['comments']} | 回复: {self.stats['replies']}\n"
                f"点赞: {self.stats['likes']} | 说说: {self.stats['posts']}\n"
                f"上次检查: {self.stats['last_check'] or '未开始'}\n"
                f"活跃对话: {len(self._state['threads'])} 条")
