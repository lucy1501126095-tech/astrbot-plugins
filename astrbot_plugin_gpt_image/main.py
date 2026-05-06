import re
import os
import base64
import aiohttp
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain, Node
from astrbot.api import logger, AstrBotConfig

@register(
    "astrbot_plugin_gpt_image",
    "Kai",
    "GPT Image 2 画图插件，支持 chat 模式和 image 模式",
    "1.4.0",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.image_provider_id = config.get("image_provider", "")
        self.model = config.get("model", "gpt-image-2")
        self.timeout = config.get("timeout", 120)
        self.last_image_url = {}
        # 新增三个配置项
        self.api_mode = config.get("api_mode", "chat")  # "chat" 或 "image"
        self.image_api_base = config.get("image_api_base", "")  # 中转站地址
        self.image_api_key = config.get("image_api_key", "")  # 中转站key

    async def _get_image_provider(self, event: AstrMessageEvent):
        """获取画图用的模型提供商实例"""
        provider_id = self.image_provider_id

        if provider_id:
            try:
                prov = await self.context.provider_manager.get_provider_by_id(provider_id)
                if prov:
                    return prov
                logger.warning(f"未找到 ID 为 {provider_id} 的模型提供商，将使用当前会话提供商")
            except Exception as e:
                logger.warning(f"获取模型提供商失败: {e}，将使用当前会话提供商")

        return self.context.get_using_provider(umo=event.unified_msg_origin)

    def _image_to_base64(self, file_path: str) -> tuple[str, str]:
        """读取本地图片文件，返回 (base64_data, mime_type)"""
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        mime_type = mime_map.get(ext, "image/png")
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return data, mime_type

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> MessageEventResult:
        """根据用户的描述生成图片。当用户想要画图、生成图片、创建图像时调用此工具。

        Args:
            prompt(str): 用于生成图片的英文描述。请将用户的描述翻译成详细的英文 prompt，包含风格、细节、构图等信息。
        """
        session_id = event.session_id or "default"
        logger.info(f"GPT Image 生成请求: {prompt}")

        prov = None
        if self.api_mode == "chat":
            prov = await self._get_image_provider(event)
            if not prov:
                yield event.plain_result("未找到可用的模型提供商，请在插件配置中选择画图提供商，或确保已配置默认提供商。")
                return

        try:
            result = await self._call_image_api(prov, prompt)

            if result:
                self.last_image_url[session_id] = {
                    "url": result,
                    "prompt": prompt,
                }

                # 发图片给用户
                if os.path.isfile(result):
                    img = Image.fromFileSystem(result)
                else:
                    img = Image.fromURL(result)
                yield event.chain_result([img])

                # 把 prompt 包成转发消息折叠发送，避免刷屏
                prompt_node = Node(
                    uin="0",
                    name="画图 Prompt",
                    content=[Plain(f"{prompt}")]
                )
                yield event.chain_result([prompt_node])

            else:
                yield event.plain_result("画图失败：API 返回的内容中未找到图片链接，可能是服务负载过高，请稍后重试。")

        except asyncio.TimeoutError:
            yield event.plain_result("画图请求超时了，画图通常需要较长时间，请稍后重试。")
        except Exception as e:
            logger.error(f"GPT Image 生成失败: {e}")
            yield event.plain_result(f"画图失败: {str(e)}")

    @filter.llm_tool(name="edit_image")
    async def edit_image(
        self, event: AstrMessageEvent, edit_instruction: str
    ) -> MessageEventResult:
        """基于上一次生成的图片进行修改。当用户想要修改刚才画的图片时调用此工具。例如"把背景换成星空"、"去掉多余的手指"。

        Args:
            edit_instruction(str): 英文的修改指令。请将用户的修改要求翻译成英文，并结合上一次的 prompt 生成新的完整描述。
        """
        session_id = event.session_id or "default"
        last = self.last_image_url.get(session_id)

        if not last:
            yield event.plain_result("没有找到上一次生成的图片记录，请先画一张图。")
            return

        prov = None
        if self.api_mode == "chat":
            prov = await self._get_image_provider(event)
            if not prov:
                yield event.plain_result("未找到可用的模型提供商，请在插件配置中选择画图提供商，或确保已配置默认提供商。")
                return

        new_prompt = edit_instruction
        logger.info(f"GPT Image 修改请求: {new_prompt}")

        try:
            result = await self._call_image_api(prov, new_prompt)

            if result:
                self.last_image_url[session_id] = {
                    "url": result,
                    "prompt": new_prompt,
                }

                # 发图片给用户
                if os.path.isfile(result):
                    img = Image.fromFileSystem(result)
                else:
                    img = Image.fromURL(result)
                yield event.chain_result([img])

                # 把 prompt 包成转发消息折叠发送
                prompt_node = Node(
                    uin="0",
                    name="修改 Prompt",
                    content=[Plain(f"原始: {last['prompt']}\n修改: {new_prompt}")]
                )
                yield event.chain_result([prompt_node])

            else:
                yield event.plain_result("修改图片失败：API 返回的内容中未找到图片链接，可能是服务负载过高，请稍后重试。")

        except asyncio.TimeoutError:
            yield event.plain_result("修改图片请求超时，请稍后重试。")
        except Exception as e:
            logger.error(f"GPT Image 修改失败: {e}")
            yield event.plain_result(f"修改图片失败: {str(e)}")

    async def _call_image_api(self, provider, prompt: str) -> str | None:
        """调用画图 API，返回图片 URL 或本地文件路径"""

        if self.api_mode == "image":
            # ===== image 模式：直接请求 /v1/images/generations =====
            url = self.image_api_base.rstrip("/") + "/v1/images/generations"
            headers = {
                "Authorization": f"Bearer {self.image_api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": self.model,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error(f"Image API 错误 ({resp.status}): {data}")
                        return None
                    if "data" in data and len(data["data"]) > 0:
                        item = data["data"][0]
                        if "url" in item and item["url"]:
                            return item["url"]
                        elif "b64_json" in item and item["b64_json"]:
                            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
                            os.makedirs(tmp_dir, exist_ok=True)
                            file_path = os.path.join(tmp_dir, f"b64_{id(prompt)}.png")
                            with open(file_path, "wb") as f:
                                f.write(base64.b64decode(item["b64_json"]))
                            return file_path
                    return None
        else:
            # ===== chat 模式：走 AstrBot provider =====
            kwargs = {}
            if self.model:
                kwargs["model"] = self.model

            llm_resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, **kwargs),
                timeout=self.timeout,
            )

            content = llm_resp.completion_text or ""
            logger.info(f"API 返回 content: {content[:200]}...")

            if "失败" in content or "error" in content.lower():
                logger.error(f"API 返回错误: {content}")
                return None

            img_pattern = r"!\[.*?\]\((https?://[^\s\)]+)\)"
            match = re.search(img_pattern, content)
            if match:
                return match.group(1)

            dl_pattern = r"\[.*?下载.*?\]\((https?://[^\s\)]+)\)"
            match = re.search(dl_pattern, content)
            if match:
                return match.group(1)

            url_pattern = r"(https?://[^\s\)\\\"]+\.(?:png|jpg|jpeg|webp|gif))"
            match = re.search(url_pattern, content)
            if match:
                return match.group(1)

            return None

    async def _download_image(self, url: str, session_id: str) -> str | None:
        """下载图片到本地临时目录"""
        try:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

            ext = ".webp"
            if ".png" in url:
                ext = ".png"
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
