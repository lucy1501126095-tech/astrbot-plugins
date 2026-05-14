# astrbot_plugin_gpt_image

GPT Image 画图插件，适配 AstrBot 框架。支持自然对话中自动调用生成图片、图生图编辑。

## 版本 2.0.2

基于官克 2.0.0 版本修复：
- 修复配置键名不一致问题（`api_key` → `image_api_key`）
- 修复 API 路径缺少 `/v1`
- 兼容中转站配置
- 修复画图请求超时问题（默认超时 600 秒）

## 安装

将插件文件夹放入 `data/plugins/` 下，重启 AstrBot。

## 配置

在插件配置中填写：
- `image_api_base`：中转站地址
- `image_api_key`：API Key
- `model`：模型名称（默认 gpt-image-2）

支持 `image` 模式和 `chat` 模式。
