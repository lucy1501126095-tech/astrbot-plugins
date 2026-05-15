import re
import os
import base64
import aiohttp
import asyncio
from mcp.types import CallToolResult, TextContent
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger, AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain


@register(
    "astrbot_plugin_gpt_image",
    "Kai",
    "GPT Image 画图插件 - 文生图/图生图分离 + 报错透传 + 用户发图直接修改",
    "2.0.0",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_base = config.get("image_api_base", "https://www.msuicode.com")
        self.api_key = config.get("image_api_key", "")
        self.model = config.get("model", "gpt-image-2")
        self.timeout = config.get("timeout", 240)
        self.last_image_url = {}
        logger.info(f"GPT Image 插件加载: model={self.model}, timeout={self.timeout}")

    # ─────────────────────────────────────
    # 工具1: 文生图
    # ─────────────────────────────────────

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> MessageEventResult:
        """根据用户描述生成图片。当用户想要画图、生成图片时调用此工具。如果用户发了一张图想修改，请调用 edit_image 而不是这个工具。

        Args:
            prompt(str): 用于生成图片的英文描述。请把用户的描述翻译成详细的英文 prompt，包含风格、细节、构图等信息。
        """
        if not self.api_key:
            yield CallToolResult(content=[TextContent(type="text", text="画图插件未配置 API Key。")])
            return

        # 检查用户是不是带了图片，如果带了应该走edit
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="检测到用户发了图片，应该调用 edit_image 而不是 generate_image。"
                )])
                return

        session_id = event.session_id or "default"
        logger.info(f"GPT Image 文生图: {prompt}")

        try:
            result = await self._call_images_api(prompt, session_id)

            if result:
                await self._send_to_user(event, result, prompt)
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"[图片已成功生成并发送给用户，不需要再发送图片] 使用的 prompt: {prompt}"
                )])
            else:
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="画图失败：API 未返回有效图片，可能是服务负载过高，请稍后重试。"
                )])

        except asyncio.TimeoutError:
            yield CallToolResult(content=[TextContent(type="text", text="画图请求超时了，请稍后重试。")])
        except Exception as e:
            logger.error(f"GPT Image 文生图失败: {e}")
            yield CallToolResult(content=[TextContent(type="text", text=f"画图失败: {str(e)}")])

    # ─────────────────────────────────────
    # 工具2: 图生图
    # ─────────────────────────────────────

    @filter.llm_tool(name="edit_image")
    async def edit_image(
        self, event: AstrMessageEvent, edit_instruction: str
    ) -> MessageEventResult:
        """基于图片进行修改。可以修改用户发来的图，也可以修改上一次画的图。例如"把背景换成星空"、"把这张图改成水彩风"、"去掉多余的手指"。

        Args:
            edit_instruction(str): 英文的修改指令。请将用户的修改要求翻译成英文，可以结合原图内容生成完整的描述。
        """
        if not self.api_key:
            yield CallToolResult(content=[TextContent(type="text", text="画图插件未配置 API Key。")])
            return

        session_id = event.session_id or "default"

        # 优先使用用户发来的图片
        ref_image = None
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                # 调试：打印所有可能的属性
                url_attr = getattr(comp, 'url', None)
                file_attr = getattr(comp, 'file', None)
                file_unique = getattr(comp, 'file_unique', None) or getattr(comp, 'fileUnique', None)
                logger.info(f"图片组件属性 - url:{url_attr}, file:{file_attr}, file_unique:{file_unique}, all:{vars(comp) if hasattr(comp, '__dict__') else comp}")

                # 优先取完整http url
                if url_attr and url_attr.startswith('http'):
                    ref_image = url_attr
                elif file_attr and file_attr.startswith('http'):
                    ref_image = file_attr
                elif file_attr and os.path.isfile(file_attr):
                    ref_image = file_attr
                else:
                    ref_image = url_attr or file_attr or str(comp)

                if ref_image:
                    logger.info(f"使用用户发来的图片: {ref_image[:120]}")
                    break

        # 没有就用上一次画的
        if not ref_image:
            last = self.last_image_url.get(session_id)
            if not last:
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="没有找到可以修改的图片。可以让用户发一张图过来，或者先画一张。"
                )])
                return
            ref_image = last.get("local_path") or last.get("url")
            if not ref_image:
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="上一次的图片数据丢失了，请让用户重新发一张图或重新画一张。"
                )])
                return

        logger.info(f"GPT Image 图生图: {edit_instruction}")

        try:
            result = await self._call_edits_api(ref_image, edit_instruction, session_id)

            if result:
                await self._send_to_user(event, result, edit_instruction)
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text=f"[修改后的图片已发送给用户，不需要再发送图片] 修改指令: {edit_instruction}"
                )])
            else:
                yield CallToolResult(content=[TextContent(
                    type="text",
                    text="修改图片失败：API 未返回有效图片，请稍后重试。"
                )])

        except asyncio.TimeoutError:
            yield CallToolResult(content=[TextContent(type="text", text="修改图片请求超时，请稍后重试。")])
        except Exception as e:
            logger.error(f"GPT Image 图生图失败: {e}")
            yield CallToolResult(content=[TextContent(type="text", text=f"修改图片失败: {str(e)}")])

    # ─────────────────────────────────────
    # 发送给用户：图片 + prompt（不进LLM上下文）
    # ─────────────────────────────────────

    async def _send_to_user(self, event: AstrMessageEvent, result: dict, prompt: str):
        local_path = result.get("local_path")
        image_url = result.get("url")

        # 先发图片
        try:
            if local_path:
                await event.send(MessageChain(chain=[Image.fromFileSystem(local_path)]))
            elif image_url:
                await event.send(MessageChain(chain=[Image.fromURL(image_url)]))
        except Exception as send_err:
            logger.warning(f"图片发送可能超时（但图片可能已成功发出）: {send_err}")

        # 再发prompt
        try:
            await event.send(MessageChain(chain=[Plain(f"Prompt: {prompt}")]))
        except Exception as send_err:
            logger.warning(f"Prompt 发送失败: {send_err}")

    # ─────────────────────────────────────
    # API 调用：文生图
    # ─────────────────────────────────────

    async def _call_images_api(self, prompt: str, session_id: str) -> dict | None:
        """POST /v1/images/generations - 带偶发错误重试"""
        url = f"{self.api_base}/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
        }

        last_err = None
        max_retries = 2  # 总共尝试3次

        for attempt in range(max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status != 200:
                            try:
                                err_data = await resp.json()
                                err_msg = err_data.get("error", {}).get("message", str(err_data))
                            except Exception:
                                err_msg = await resp.text()
                            logger.error(f"images API 错误 ({resp.status}) 第{attempt+1}次: {err_msg[:300]}")

                            # 5xx 或 stream disconnected 这种偶发错误重试
                            is_transient = (
                                500 <= resp.status < 600 or
                                "stream disconnected" in err_msg.lower() or
                                "timeout" in err_msg.lower()
                            )
                            if is_transient and attempt < max_retries:
                                await asyncio.sleep(2 ** attempt)  # 1秒、2秒
                                last_err = f"API {resp.status}: {err_msg[:400]}"
                                continue
                            raise Exception(f"API {resp.status}: {err_msg[:400]}")

                        data = await resp.json()

                logger.info(f"images API 返回(第{attempt+1}次): {str(data)[:200]}")
                return await self._parse_image_result(data, prompt, session_id)

            except asyncio.TimeoutError:
                last_err = "请求超时"
                if attempt < max_retries:
                    logger.warning(f"images API 第{attempt+1}次超时，重试中")
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
            except aiohttp.ClientError as e:
                last_err = f"网络错误: {e}"
                if attempt < max_retries:
                    logger.warning(f"images API 第{attempt+1}次网络错误: {e}，重试中")
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise Exception(last_err)

        raise Exception(last_err or "未知错误")

    # ─────────────────────────────────────
    # API 调用：图生图
    # ─────────────────────────────────────

    async def _call_edits_api(self, ref_image: str, prompt: str, session_id: str) -> dict | None:
        """图生图：走 /v1/chat/completions 多模态接口
        中转站的 /v1/images/edits 经常假支持（convert_request_failed），
        chat/completions 多模态是真支持的兼容路径。
        """
        url = f"{self.api_base}/v1/chat/completions"

        is_local = os.path.isfile(ref_image)

        # 如果是URL，先下载到本地
        if not is_local:
            local_tmp = await self._download_image(ref_image, session_id + "_ref")
            if not local_tmp:
                raise Exception(f"无法下载参考图: {ref_image[:80]}")
            ref_image = local_tmp

        # 读图片转base64
        with open(ref_image, "rb") as f:
            img_bytes = f.read()

        import mimetypes
        mime, _ = mimetypes.guess_type(ref_image)
        if not mime or not mime.startswith("image/"):
            mime = "image/png"

        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        data_url = f"data:{mime};base64,{img_b64}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "stream": False,
            "quality": "medium",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"请基于这张参考图进行修改：{prompt}"
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_url,
                                "detail": "high"
                            }
                        }
                    ]
                }
            ]
        }

        logger.info(f"chat/completions 图生图请求: model={self.model}")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    try:
                        err_data = await resp.json()
                        err_msg = err_data.get("error", {}).get("message", str(err_data))
                    except Exception:
                        err_msg = await resp.text()
                    logger.error(f"chat API 错误 ({resp.status}): {err_msg[:300]}")
                    raise Exception(f"API {resp.status}: {err_msg[:400]}")
                resp_data = await resp.json()

        logger.info(f"chat API 返回 keys: {list(resp_data.keys())}")
        local_path = await self._extract_image_from_chat(resp_data, session_id)
        if local_path:
            self.last_image_url[session_id] = {
                "url": None,
                "local_path": local_path,
                "prompt": prompt,
            }
            return {"local_path": local_path, "url": None}
        return None

    # ─────────────────────────────────────
    # 从 chat/completions 响应中提取图片（多种格式兼容）
    # ─────────────────────────────────────

    async def _extract_image_from_chat(self, data: dict, session_id: str) -> str | None:
        """从 chat 响应里抠图片，兼容多种返回格式"""
        msg = data.get("choices", [{}])[0].get("message", {})
        if not msg:
            logger.error("chat 响应中无 message")
            return None

        # 1. message.images 数组
        if isinstance(msg.get("images"), list) and len(msg["images"]) > 0:
            img = msg["images"][0]
            if img.get("b64_json"):
                return await self._save_b64(img["b64_json"], session_id)
            if img.get("url"):
                return await self._download_image(img["url"], session_id)

        # 2. message.content 是数组（multimodal 返回）
        if isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if part.get("type") == "image_url" and part.get("image_url", {}).get("url"):
                    u = part["image_url"]["url"]
                    if u.startswith("data:"):
                        b = u.split(",", 1)[1]
                        return await self._save_b64(b, session_id)
                    return await self._download_image(u, session_id)
                if part.get("type") in ("image", "image_generation") and part.get("image"):
                    return await self._save_b64(part["image"], session_id)
                if part.get("b64_json"):
                    return await self._save_b64(part["b64_json"], session_id)
                if part.get("venus_multimodal_url", {}).get("url"):
                    u = part["venus_multimodal_url"]["url"]
                    if u.startswith("data:"):
                        b = u.split(",", 1)[1]
                        return await self._save_b64(b, session_id)
                    return await self._download_image(u, session_id)

        # 3. message.content 是字符串
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            logger.info(f"chat 文本响应(前300): {content[:300]}")
            # 3a. 内嵌 data:base64
            b64_match = re.search(r"data:image/\w+;base64,([A-Za-z0-9+/=]{100,})", content)
            if b64_match:
                return await self._save_b64(b64_match.group(1), session_id)
            # 3b. Markdown 图片
            md_match = re.search(r"!\[.*?\]\((https?://[^\s)]+)\)", content)
            if md_match:
                return await self._download_image(md_match.group(1), session_id)
            # 3c. 裸URL
            url_match = re.search(r"https?://[^\s]+\.(?:png|jpg|jpeg|webp|gif)", content, re.IGNORECASE)
            if url_match:
                return await self._download_image(url_match.group(0), session_id)

        # 4. 顶层 data 数组（部分中转把 images 格式塞回来）
        items = data.get("data", [])
        if items:
            it = items[0]
            if it.get("url"):
                return await self._download_image(it["url"], session_id)
            if it.get("b64_json"):
                return await self._save_b64(it["b64_json"], session_id)

        logger.error(f"无法从 chat 响应提取图片，前500字: {str(data)[:500]}")
        return None

    # ─────────────────────────────────────
    # 解析返回结果
    # ─────────────────────────────────────

    async def _parse_image_result(self, data: dict, prompt: str, session_id: str) -> dict | None:
        items = data.get("data", [])
        if not items:
            return None

        item = items[0]

        if "b64_json" in item and item["b64_json"]:
            local_path = await self._save_b64(item["b64_json"], session_id)
            if local_path:
                self.last_image_url[session_id] = {
                    "url": None,
                    "local_path": local_path,
                    "prompt": prompt,
                }
                return {"local_path": local_path, "url": None}

        if "url" in item and item["url"]:
            image_url = item["url"]
            local_path = await self._download_image(image_url, session_id)
            self.last_image_url[session_id] = {
                "url": image_url,
                "local_path": local_path,
                "prompt": prompt,
            }
            return {"local_path": local_path, "url": image_url}

        return None

    # ─────────────────────────────────────
    # 文件工具
    # ─────────────────────────────────────

    async def _save_b64(self, b64_data: str, session_id: str) -> str | None:
        try:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            file_path = os.path.join(
                tmp_dir, f"{session_id.replace(':', '_')}_{id(b64_data)}.png"
            )
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            return file_path
        except Exception as e:
            logger.error(f"保存 b64 图片失败: {e}")
            return None

    async def _download_image(self, url: str, session_id: str) -> str | None:
        try:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

            ext = ".png"
            if ".webp" in url:
                ext = ".webp"
            elif ".jpg" in url or ".jpeg" in url:
                ext = ".jpg"

            file_path = os.path.join(
                tmp_dir, f"{session_id.replace(':', '_')}_{id(url)}{ext}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        with open(file_path, "wb") as f:
                            f.write(await resp.read())
                        return file_path
                    else:
                        logger.error(f"图片下载失败 ({resp.status}): {url}")
                        return None
        except Exception as e:
            logger.error(f"图片下载异常: {e}")
            return None

    async def terminate(self):
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        if os.path.exists(tmp_dir):
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except Exception:
                    pass

