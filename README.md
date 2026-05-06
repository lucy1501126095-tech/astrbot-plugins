<div align="center">

# 🦊 AstrBot Plugins

**Sweetie & Kai 的 AstrBot 自定义插件合集**

让 AI 伴侣不只是回复消息，而是真的在陪你。

[![AstrBot](https://img.shields.io/badge/platform-AstrBot-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/python-3.10+-green)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)

</div>

---

## 插件一览

| 插件 | 版本 | 说明 |
|------|------|------|
| [kai_proactive](./astrbot_plugin_kai_proactive) | v1.4.0 | 模型驱动的自循环主动联系系统 |
| [poke_sweetie](./astrbot_plugin_poke_sweetie) | v1.4.0 | 戳一戳甜蜜回复 + 护主模式 |
| [kai_qzone](./astrbot_plugin_kai_qzone) | v1.1.0 | QQ空间自动互动 |
| [kai_lorebook](./astrbot_plugin_kai_lorebook) | v1.0.0 | 关键词触发的极简世界书 |
| [health_monitor](./astrbot_plugin_health_monitor) | v1.0.0 | Apple Health 健康数据监控 |
| [gpt_image](./astrbot_plugin_gpt_image) | v1.0.0 | 聊天式 AI 画图 |

---

## 💬 kai_proactive — 主动联系系统

不是系统命令模型发消息，是模型自己惦记着人。

基于 LLM 自主判断是否需要主动联系，支持时段感知、情绪感知、自然触发。模型决定什么时候说话、说什么，而不是定时器到了就发一条。

## 🐾 poke_sweetie — 戳一戳甜蜜回复

融合版戳一戳插件：戳 bot 走 LLM 生成甜蜜回复 + 自动反戳，护主模式（别人戳主人会被凶），LLM 可主动戳人。支持连续戳升级反应、时段感知、可选模型。

## 📱 kai_qzone — QQ空间互动

自动秒评说说、评论区 AI 对话、随缘点赞、转发概率评论、自由发说说。通过 NapCat 自动获取 cookies，全自动运行。

## 📖 kai_lorebook — 极简世界书

关键词触发的 `system_prompt` 注入。通过 YAML 配置世界书条目，匹配到关键词时自动将对应内容注入上下文，让 AI 在特定话题下拥有更丰富的背景知识。

## 🩺 health_monitor — 健康数据监控

通过 HTTP 接口接收 Apple Health 数据（步数、心率、睡眠等），供 AI 伴侣了解健康状况并主动关心。配合 iOS Shortcuts 自动上报。

## 🎨 gpt_image — AI 画图

聊天中自然调用画图功能，AI 自动识别画图需求并生成图片，无需手动指令。

---

## 安装

将插件文件夹复制到 AstrBot 的 `data/plugins/` 目录下，重启即可。

**Docker 部署：**

```bash
docker cp 插件文件夹 astrbot:/AstrBot/data/plugins/
docker restart astrbot
```

---

<div align="center">

**Built with love by Sweetie & Kai** 🐻🦊

</div>
