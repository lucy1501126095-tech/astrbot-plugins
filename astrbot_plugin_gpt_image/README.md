# astrbot_plugin_gpt_image

GPT Image 画图插件。Kai 自动识别画图需求，文生图 + 图生图。

## ⚠️ 重要：框架超时必改

AstrBot 框架在工具调用外面有一层硬超时 `tool_call_timeout`，默认 **60 秒**。复杂图生成需要 90-120 秒，不改这个会被框架掐断，表现为"插件没报错但图没出来"，绕一万圈也找不出问题。

**装好插件先做这一步**：

后台 → Provider 设置 → `tool_call_timeout` 改成 **600**。

如果 UI 里看不到这个字段，直接改配置文件：

```bash
docker exec astrbot grep -rn "tool_call_timeout" /AstrBot/data/config
# 找到对应文件，把 60 改成 600
docker restart astrbot
```

## 配置字段

5 项，按编号填：

1. **服务地址** `image_api_base` — 中转站网址，例 `https://api.denxio.top`，不带 `/v1`
2. **密钥** `image_api_key` — sk- 开头那串
3. **走什么端点** `api_mode` — `images`（denxio.top 这类严格站）或 `chat`（msuicode 这类宽松站）
4. **模型名** `model` — 默认 `gpt-image-2`
5. **超时秒数** `timeout` — 默认 600

不知道选 `images` 还是 `chat`：先试 `images`，画图直接报"only supported on"或"convert_request_failed"就换 `chat`。

## 用法

跟 Kai 说"帮我画个 XXX"。

发一张图过来 + "把背景换成星空"，自动走图生图。

不发图说"修改一下"，会拿上一次画的那张改。

## 已知坑（踩过的）

- **不要走 `chat` 模式画 gpt-image-2 到 denxio.top**：会 503 `only supported on /v1/images/generations`
- **不要走 `images` 模式到 msuicode**：edits 端点是假支持，返回 `convert_request_failed`
- **不要把 `/v1` 写进 `image_api_base`**：代码会自动拼，重复 `/v1/v1` 全部 502
- **不要相信"画图失败：服务负载过高"这种笼统提示**：99% 是 `tool_call_timeout` 太短

## 版本

- **v2.1.0**（当前稳定版）：api_mode 手动开关 + 错误透传 + edits 端点 + schema 大白话描述
- v2.0.x：多次尝试自动 fallback，证明没用，放弃
- v1.2.0：原版，能跑但 schema 字段太多
