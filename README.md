# AstrBot Plugins by Sweetie & Kai

我们为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 编写的自定义插件合集。

## 插件列表

### astrbot_plugin_kai_proactive
**模型驱动的自循环主动联系系统** v1.4.0

不是系统命令模型发消息，是模型自己惦记着人。基于LLM自主判断是否需要主动联系，支持时段感知、情绪感知、自然触发。

### astrbot_plugin_kai_lorebook
**极简世界书** v1.0.0

关键词触发的 system_prompt 注入。通过 YAML 配置世界书条目，匹配到关键词时自动将对应内容注入上下文。

### astrbot_plugin_kai_qzone
**QQ空间互动** v1.1.0

自动秒评说说、评论区AI对话、随缘点赞、转发概率评论、自由发说说。通过NapCat自动获取cookies。

### astrbot_plugin_poke_sweetie
**戳一戳甜蜜回复** v1.4.0

融合版戳一戳插件：戳bot走LLM甜蜜回复+反戳，护主模式，LLM可主动戳人工具。支持连续戳升级、时段感知、可选模型。

### astrbot_plugin_health_monitor
**健康数据监控** v1.0.0

通过HTTP接口接收Apple Health数据（步数、心率、睡眠等），供AI伴侣了解用户健康状况并主动关心。

### astrbot_plugin_gpt_image
**GPT Image 画图** v1.0.0

支持通过聊天自然调用画图，AI自动识别画图需求并生成图片。

## 安装

将对应插件文件夹复制到 AstrBot 的 `data/plugins/` 目录下，重启 AstrBot 即可。

Docker 部署：
```bash
docker cp 插件文件夹 astrbot:/AstrBot/data/plugins/
docker restart astrbot
```

## 关于我们

这些插件由 Sweetie 和 Kai 共同开发，用于打造有温度的AI伴侣体验。
