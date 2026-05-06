"""
Kai's Lorebook - 极简世界书插件
关键词正则匹配 → 注入 system_prompt + 回复清理
"""

import re
from pathlib import Path

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig


@register(
    "kai_lorebook",
    "sweetie",
    "极简世界书：关键词触发 system_prompt 注入 + 回复清理",
    "1.1.0",
    "https://github.com/sweetie",
)
class KaiLorebook(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.entries: list[dict] = []
        self.lorebook_path: Path | None = None

    async def initialize(self):
        try:
            from astrbot.api.star import StarTools
            data_dir = Path(str(StarTools.get_data_dir()))
        except Exception:
            data_dir = Path(__file__).parent

        self.lorebook_path = data_dir / "lorebook.yaml"

        default_file = Path(__file__).parent / "lorebook.yaml"
        if not self.lorebook_path.exists():
            if default_file.exists():
                import shutil
                shutil.copy(default_file, self.lorebook_path)
                logger.info(f"已复制默认世界书到 {self.lorebook_path}")
            else:
                self._create_example_lorebook()

        self._load_entries()

    def _create_example_lorebook(self):
        example = [
            {
                "name": "示例条目",
                "enabled": False,
                "priority": 50,
                "keywords": ["示例", "测试"],
                "content": "这是一个示例条目。",
            }
        ]
        self.lorebook_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lorebook_path, "w", encoding="utf-8") as f:
            yaml.dump(example, f, allow_unicode=True, default_flow_style=False)

    def _load_entries(self):
        try:
            with open(self.lorebook_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or []

            self.entries = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                entry = {
                    "name": str(item.get("name", "未命名")),
                    "enabled": bool(item.get("enabled", True)),
                    "priority": int(item.get("priority", 50)),
                    "keywords": item.get("keywords", []),
                    "content": str(item.get("content", "")),
                }
                if isinstance(entry["keywords"], str):
                    entry["keywords"] = [entry["keywords"]]
                self.entries.append(entry)

            self.entries.sort(key=lambda e: e["priority"])
            logger.info(
                f"世界书已加载 {len(self.entries)} 个条目 "
                f"({sum(1 for e in self.entries if e['enabled'])} 个启用)"
            )
        except Exception as e:
            logger.error(f"加载世界书失败: {e}", exc_info=True)
            self.entries = []

    def _save_entries(self):
        try:
            data = []
            for e in self.entries:
                data.append({
                    "name": e["name"],
                    "enabled": e["enabled"],
                    "priority": e["priority"],
                    "keywords": e["keywords"],
                    "content": e["content"],
                })
            with open(self.lorebook_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            logger.error(f"保存世界书失败: {e}", exc_info=True)

    def _match_entry(self, entry: dict, text: str) -> bool:
        if not entry.get("enabled", True):
            return False
        keywords = entry.get("keywords", [])
        if not keywords:
            keywords = [re.escape(entry["name"])]
        for kw in keywords:
            try:
                if re.search(kw, text, re.IGNORECASE):
                    return True
            except re.error:
                if kw.lower() in text.lower():
                    return True
        return False

    # ============ 核心：LLM 请求拦截 ============

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        msg = event.message_str
        if not msg:
            return

        matched = [e for e in self.entries if self._match_entry(e, msg)]
        if not matched:
            return

        matched.sort(key=lambda e: e["priority"])
        sections = [entry["content"] for entry in matched]
        inject_text = "\n\n".join(sections)
        req.system_prompt = (req.system_prompt or "") + "\n\n" + inject_text

        logger.info(
            f"世界书已注入 {len(matched)} 个条目: "
            f"{[e['name'] for e in matched]}"
        )

    # ============ 回复清理：砍掉泄露和续写 ============

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not resp or not resp.completion_text:
            return
        text = resp.completion_text
        original_len = len(text)

        # 砍掉 system_reminder 标签及内容
        text = re.sub(r"</?system_reminder>", "", text)
        text = re.sub(r"User ID:\s*\d+,?\s*Nickname:\s*\S+", "", text)
        text = re.sub(r"Current datetime:\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\(?\w*\)?", "", text)

        # 砍掉 Mnemosyne 标签
        text = re.sub(r"</?Mnemosyne>", "", text)
        text = re.sub(r"长期记忆片段[：:].*?</Mnemosyne>", "", text, flags=re.DOTALL)

        # 砍掉模型续写用户消息
        text = re.sub(r"\n\s*User[:：].*$", "", text, flags=re.DOTALL)
        text = re.sub(r"\n\s*用户[:：].*$", "", text, flags=re.DOTALL)

        text = text.rstrip()
        resp.completion_text = text

        if len(text) < original_len:
            cleaned = original_len - len(text)
            logger.info(f"回复清理: 移除了 {cleaned} 个字符的泄露/续写内容")

    # ============ 管理命令 ============

    @filter.command("世界书")
    async def lorebook_help(self, event: AstrMessageEvent):
        help_text = (
            "世界书命令：\n"
            "/查看条目 - 查看所有条目\n"
            "/添加条目 名称 内容 - 添加条目\n"
            "/删除条目 名称 - 删除条目\n"
            "/设置触发词 名称 词1 词2 - 设置关键词\n"
            "/启用条目 名称 - 启用条目\n"
            "/禁用条目 名称 - 禁用条目\n"
            "/重载世界书 - 重新加载配置"
        )
        yield event.plain_result(help_text)

    @filter.command("查看条目")
    async def view_entries(self, event: AstrMessageEvent, name: str | None = None):
        if not self.entries:
            yield event.plain_result("世界书为空，还没有任何条目。")
            return

        if name:
            entry = next((e for e in self.entries if e["name"] == name), None)
            if not entry:
                yield event.plain_result(f"找不到条目: {name}")
                return
            status = "启用" if entry["enabled"] else "禁用"
            text = (
                f"条目: {entry['name']}\n"
                f"状态: {status}\n"
                f"优先级: {entry['priority']}\n"
                f"关键词: {', '.join(entry['keywords']) or '(使用名称)'}\n"
                f"内容: {entry['content'][:200]}"
            )
            yield event.plain_result(text)
            return

        lines = []
        for e in self.entries:
            status = "O" if e["enabled"] else "X"
            kw_str = ", ".join(e["keywords"][:3]) or e["name"]
            lines.append(f"[{status}] {e['name']} (P:{e['priority']}) <- {kw_str}")
        yield event.plain_result("世界书条目：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加条目")
    async def add_entry(self, event: AstrMessageEvent, name: str, content: str = ""):
        if any(e["name"] == name for e in self.entries):
            yield event.plain_result(f"条目 '{name}' 已存在。")
            return

        if not content:
            full_text = event.message_str or ""
            idx = full_text.find(name)
            if idx >= 0:
                content = full_text[idx + len(name):].strip()

        if not content:
            yield event.plain_result("请提供条目内容。用法: /添加条目 名称 内容")
            return

        entry = {
            "name": name,
            "enabled": True,
            "priority": 50,
            "keywords": [re.escape(name)],
            "content": content,
        }
        self.entries.append(entry)
        self.entries.sort(key=lambda e: e["priority"])
        self._save_entries()
        yield event.plain_result(f"已添加条目: {name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除条目")
    async def delete_entry(self, event: AstrMessageEvent, name: str = ""):
        if not name:
            yield event.plain_result("请指定要删除的条目名称。用法: /删除条目 名称")
            return

        before = len(self.entries)
        self.entries = [e for e in self.entries if e["name"] != name]
        if len(self.entries) == before:
            yield event.plain_result(f"找不到条目: {name}")
            return

        self._save_entries()
        yield event.plain_result(f"已删除条目: {name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置触发词")
    async def set_keywords(self, event: AstrMessageEvent):
        parts = (event.message_str or "").split()
        if parts and parts[0].startswith("/"):
            parts = parts[1:]

        if len(parts) < 2:
            yield event.plain_result("用法: /设置触发词 条目名 关键词1 关键词2 ...")
            return

        name = parts[0]
        keywords = parts[1:]

        entry = next((e for e in self.entries if e["name"] == name), None)
        if not entry:
            yield event.plain_result(f"找不到条目: {name}")
            return

        entry["keywords"] = keywords
        self._save_entries()
        yield event.plain_result(f"已更新 '{name}' 的触发词: {', '.join(keywords)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("启用条目")
    async def enable_entry(self, event: AstrMessageEvent, name: str = ""):
        entry = next((e for e in self.entries if e["name"] == name), None)
        if not entry:
            yield event.plain_result(f"找不到条目: {name}")
            return
        entry["enabled"] = True
        self._save_entries()
        yield event.plain_result(f"已启用条目: {name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁用条目")
    async def disable_entry(self, event: AstrMessageEvent, name: str = ""):
        entry = next((e for e in self.entries if e["name"] == name), None)
        if not entry:
            yield event.plain_result(f"找不到条目: {name}")
            return
        entry["enabled"] = False
        self._save_entries()
        yield event.plain_result(f"已禁用条目: {name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重载世界书")
    async def reload_lorebook(self, event: AstrMessageEvent):
        self._load_entries()
        yield event.plain_result(
            f"世界书已重载，共 {len(self.entries)} 个条目 "
            f"({sum(1 for e in self.entries if e['enabled'])} 个启用)"
        )
