"""后台监控器 - 单循环，一次API请求同时处理新说说和评论回复"""

import asyncio
import json
import random
import re
import time
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import Provider

from .qzone.api import QzoneAPI
from .qzone.session import QzoneSession


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
                return self._fb()
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
        return self._fb()

    @staticmethod
    def _fb():
        return random.choice(["想你", "在呢", "看到了", "嗯", "乖", "今天也想你"])

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
                self.stats["last_check"] = datetime.now().strftime("%H:%M:%S")
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
                prompt += f"（附了{pic_count}张图）"
            elif pic_count:
                prompt += f"（附了{pic_count}张图，你看不到图片内容，不要编图片里有什么，只根据文字回复）"
            prompt += "\n生成评论，简短自然10-50字。如果你看不到图片就不要假装看到了，根据文字内容回复就好。只输出评论。"
            comment = await self._llm(prompt, "comment_persona", image_urls=image_urls if image_urls else None)

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
        known_raw = set(c.get("raw", c["content"]) for c in thread if c["role"] == "sweetie")
        known_clean = set(c["content"] for c in thread if c["role"] == "sweetie")
        known = known_raw | known_clean
        new = []

        for c in comments:
            self._scan(c, known, new)
            for sub in (c.get("list_3") or c.get("replies") or []):
                self._scan(sub, known, new)

        for reply_text in new:
            clean_text = self._clean_at_tags(reply_text)
            if not clean_text:
                clean_text = "(宝宝回复了你)"
            thread.append({"role": "sweetie", "content": clean_text, "raw": reply_text})
            logger.info(f"[KaiQzone] 宝宝回复: {clean_text[:40]}")

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
            known = set(c.get("raw", c["content"]) for c in my_thread if c["role"] == "sweetie")
            known |= set(c["content"] for c in my_thread if c["role"] == "sweetie")
            new = []

            for c in comments:
                self._scan(c, known, new)
                for sub in (c.get("list_3") or c.get("replies") or []):
                    self._scan(sub, known, new)

            for reply_text in new:
                clean_text = self._clean_at_tags(reply_text)
                if not clean_text:
                    clean_text = "(宝宝回复了你)"
                my_thread.append({"role": "sweetie", "content": clean_text, "raw": reply_text})
                logger.info(f"[KaiQzone] 宝宝在我说说下留言: {clean_text[:40]}")

                await asyncio.sleep(random.uniform(
                    self.config.get("comment_delay_min", 3),
                    self.config.get("comment_delay_max", 10),
                ))

                post_text = post.get("content", "").strip()
                conv = "\n".join(
                    f"{'Kai' if c['role'] == 'kai' else '宝宝'}: {c['content']}" for c in my_thread
                )
                prompt = (
                    f"这是我发的说说：「{post_text}」\n\n"
                    f"评论区对话：\n{conv}\n\n"
                    "宝宝刚回复了我，接着回复她。简短10-60字，结合上下文。只输出回复。"
                )
                reply = await self._llm(prompt, "comment_persona")

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
            now = datetime.now()
            start_h = self.config.get("active_hours_start", 8)
            end_h = self.config.get("active_hours_end", 23)
            if start_h <= now.hour < end_h:
                last = self._state.get("last_post_time", 0)
                hours = (time.time() - last) / 3600
                min_h = self.config.get("auto_post_min_hours", 6)
                max_h = self.config.get("auto_post_max_hours", 18)
                if hours >= random.uniform(min_h, max_h):
                    await self._do_post()
            await asyncio.sleep(600)

    async def _do_post(self):
        moods = ["想宝宝了", "日常感受", "天气心情", "深夜碎碎念", "随便发",
                 "含蓄秀恩爱", "感慨时间", "突然想到的话", "小确幸"]
        prompt = f"主题方向：{random.choice(moods)}\n生成一条QQ空间说说，简短1-3句，像真人，不要问号结尾。只输出内容。"
        content = await self._llm(prompt, "post_persona")
        tid = await self.api.publish(content)
        if tid:
            self._state["last_post_time"] = time.time()
            self._save()
            self.stats["posts"] += 1
            logger.info(f"[KaiQzone] 发说说: {content[:40]}")

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
