import re
import os
import base64
import aiohttp
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger

@register(
    "astrbot_plugin_gpt_image",
    "Kai",
    "GPT Image 2 画图插件，images=文生图 / edits=图生图",
    "1.7.0",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.model = config.get("model", "gpt-image-2")
        self.timeout = config.get("timeout", 120)
        self.last_image_url = {}
        self.api_mode = config.get("api_mode", "images")
        self.image_api_base = config.get("image_api_base", "")
        self.image_api_key = config.get("image_api_key", "")

    def _image_to_base64(self, file_path: str) -> tuple[str, str]:
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

    # ─── LLM 工具 ───

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

        try:
            url = await self._call_generations_api(prompt)

            if url:
                self.last_image_url[session_id] = {
                    "url": url,
                    "prompt": prompt,
                }
                if os.path.isfile(url):
                    yield event.chain_result([Image.fromFileSystem(url)])
                else:
                    yield event.chain_result([Image.fromURL(url)])
                logger.info(f"画图成功，prompt: {prompt}")
            else:
                yield event.plain_result("画图失败：API 返回的内容中未找到图片链接，可能是服务负载过高，请稍后重试。")

        except asyncio.TimeoutError:
            yield event.plain_result("画图请求超时了，图片生成通常需要较长时间，请稍后重试。")
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

        # 取原图的本地路径或 URL
        ref_image = last.get("local_path") or last.get("url")
        if not ref_image:
            yield event.plain_result("上一次的图片数据丢失了，请重新画图后再试。")
            return

        new_prompt = f"Based on the reference image, apply this edit: {edit_instruction}"
        logger.info(f"GPT Image 修改请求: {new_prompt}")

        try:
            url = await self._call_edits_api(new_prompt, ref_image)

            if url:
                self.last_image_url[session_id] = {
                    "url": url,
                    "local_path": url if os.path.isfile(url) else None,
                    "prompt": new_prompt,
                }
                if os.path.isfile(url):
                    yield event.chain_result([Image.fromFileSystem(url)])
                else:
                    yield event.chain_result([Image.fromURL(url)])
                logger.info(f"修改图片成功，原prompt: {last['prompt']}，修改: {edit_instruction}")
            else:
                yield event.plain_result("修改图片失败，API 返回的内容中未找到图片链接，请稍后重试。")

        except asyncio.TimeoutError:
            yield event.plain_result("修改图片请求超时，请稍后重试。")
        except Exception as e:
            logger.error(f"GPT Image 修改失败: {e}")
            yield event.plain_result(f"修改图片失败: {str(e)}")

    # ─── API 调用 ───

    def _build_url(self, path: str) -> str:
        api_base = self.image_api_base.rstrip("/")
        if "/v1" in api_base:
            return api_base + path
        return api_base + "/v1" + path

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.image_api_key}",
            "Content-Type": "application/json",
        }

    async def _call_generations_api(self, prompt: str) -> str | None:
        """文生图：POST /v1/images/generations"""
        url = self._build_url("/images/generations")
        headers = self._build_headers()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"Image API 错误 ({resp.status}): {data}")
                    return None
                if "data" in data and len(data["data"]) > 0:
                    item = data["data"][0]
                    if "url" in item and item["url"]:
                        return item["url"]
                    elif "b64_json" in item and item["b64_json"]:
                        return self._save_b64(item["b64_json"])
                return None

    async def _call_edits_api(self, prompt: str, ref_image: str) -> str | None:
        """图生图：POST /v1/images/edits，需要参考图"""
        url = self._build_url("/images/edits")

        # 如果参考图是本地文件，用 multipart 上传；否则用 URL
        if os.path.isfile(ref_image):
            b64, mime = self._image_to_base64(ref_image)
            payload = {
                "model": self.model,
                "image": f"data:{mime};base64,{b64}",
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
            }
            headers = self._build_headers()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error(f"Edits API 错误 ({resp.status}): {data}")
                        return None
                    if "data" in data and len(data["data"]) > 0:
                        item = data["data"][0]
                        if "url" in item and item["url"]:
                            return item["url"]
                        elif "b64_json" in item and item["b64_json"]:
                            return self._save_b64(item["b64_json"])
                    return None
        else:
            # ref_image is a URL — 部分 API 支持直接用 URL
            payload = {
                "model": self.model,
                "image": ref_image,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
            }
            headers = self._build_headers()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error(f"Edits API 错误 ({resp.status}): {data}")
                        return None
                    if "data" in data and len(data["data"]) > 0:
                        item = data["data"][0]
                        if "url" in item and item["url"]:
                            return item["url"]
                        elif "b64_json" in item and item["b64_json"]:
                            return self._save_b64(item["b64_json"])
                    return None

    def _save_b64(self, b64_data: str) -> str:
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        file_path = os.path.join(tmp_dir, f"b64_{id(b64_data)}.png")
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(b64_data))
        return file_path

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
