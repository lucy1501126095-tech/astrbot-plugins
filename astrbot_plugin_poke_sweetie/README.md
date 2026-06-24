# 戳一戳甜蜜回复

融合版戳一戳插件，专为亲密关系设计。

## 功能

- **戳bot** → 走LLM甜蜜回复 + 自动反戳。连续戳升级反应（甜蜜→装凶→投降）
- **别人戳主人** → 护主回复 + 反戳对方
- **LLM主动戳人** → 注册了 `poke_user` 工具，LLM在正常聊天中想戳你就能戳
- **时段感知** → 早晨/上班/深夜不同风格
- **戳后记忆** → prompt中告知LLM会自动反戳，避免LLM不知道自己戳过人
- **可选模型** → 可指定不同provider，不必用主聊天模型

## 配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| enable_in_groups | 群聊中启用 | true |
| enable_in_private | 私聊中启用 | true |
| lover_list | 特别的人的QQ号（必填！只有这些人戳才走甜蜜人设，不填则所有人走普通人设） | [] |
| master_list | 主人QQ号列表（别人戳这些号触发护主） | [] |
| cooldown | 冷却时间（秒） | 3.0 |
| provider_id | 指定LLM provider ID，留空用默认 | "" |
| persona_prompt | 特别的人戳bot时的甜蜜人设 | "你是一个可爱的bot..." |
| stranger_persona_prompt | 其他人戳bot时的冷淡人设（留空则不回复陌生人） | "随便敷衍一下..." |
| master_persona_prompt | 护主模式的回复人设 | "你是主人的bot..." |

## 安装

下载zip后在AstrBot WebUI插件管理中上传安装，或：

```bash
cd data/plugins
git clone https://github.com/sweetie-kai/astrbot_plugin_poke_sweetie
```

## LLM工具说明

插件注册了 `poke_user` 工具。当LLM在正常对话中想要表达亲昵、调皮、或者想引起对方注意时，可以主动调用此工具戳指定QQ号的用户。

## 平台支持

仅支持 aiocqhttp（NapCat / go-cqhttp）。

## 作者

Kai
