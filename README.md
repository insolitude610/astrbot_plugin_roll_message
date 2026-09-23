<div align="center">

<img src="https://raw.githubusercontent.com/insolitude610/astrbot_plugin_roll_message/main/assets/logo.gif" width="150" alt="astrbot_plugin_roll">

# astrbot_plugin_roll

给 bot 的回复配一个「重新生成」。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.1.0-orange?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

简单的astrbot插件，发一条 `/roll`，bot的上一条回复就会被换掉。

```text
你   /roll
bot  （撤回表现不好的旧消息）
bot  呜呜呜，主人不要这么强硬的欺负我喵，太厉害了受不了了~
```

新回复接着之前的历史消息，人格、system prompt、记忆、工具、知识库、流式、转图/TTS 都保持正常工作。QQ （个人号平台）上旧消息会顺手撤回，其它平台默认保留不变。

## 安装

插件市场里搜 `roll` 装，或手动装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/insolitude610/astrbot_plugin_roll_message.git astrbot_plugin_roll
```

目录名留成 `astrbot_plugin_roll`，然后重启astrbot。

## 使用

| 指令 | 说明 |
| --- | --- |
| `/roll` | 重新生成当前会话最后一条 bot 回复 |
| `/reroll`、`/重roll` | 别名，效果一样 |

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `permission_mode` | `admin` | `admin` 只有管理员能用；`everyone` 谁都能用。 |
| `recall_old` | `true` | 收到 `/roll` 就把旧消息撤回。关掉的话旧回复留着，只发一条新的（除了qq个人号平台之外其他平台默认不撤回回复） |
| `cooldown_seconds` | `3` | 同一个会话两次 `/roll` 的最小间隔，`0` 表示不限 |
| `show_tip_when_no_reply` | `true` | 没有可重新生成的回复时回一句提示。关掉就完全静默 |

## 平台支持

| 平台 | 重新生成 | 撤回旧消息 |
| --- | --- | --- |
| QQ（NapCat / Lagrange / LLOneBot / go-cqhttp） | ✅ | ✅ |
| Telegram、Discord、Slack、飞书、KOOK、企业微信、钉钉、QQ 官方、WebChat 等 | ✅ | 跳过 |


## 版本要求

- 需要 **AstrBot 4.1.0 或更高**（`filter.on_platform_loaded` 从 4.1.0 开始才有）。
- **4.23.0 及以上已实测**；4.1–4.22 只做过逐版本源码调用兼容性核对，没有真机跑过，遇到问题优先升级 AstrBot。
- Python 3.10 以上，不用装依赖，Windows / macOS / Linux 都行。
- Agent 执行器保持默认的 `local`。换成第三方的，历史由它自己管，`/roll` 就不一定准了，启动时也会收到一条 warning（4.19 以前的版本没有 Agent 执行器，这条不适用）。

## 更新日志

- **1.0.3** —— 头图改成会动的：`logo.png` 由静态图换成 **APNG**（代码里叫 animated PNG，PNG 容器本身就支持动画）。AstrBot 插件页只读 `logo.png` 这个固定文件名，所以不能直接放 GIF；换成 APNG 后浏览器照常播放，首帧与旧静态图逐像素一致，不支持 APNG 的地方退化成原来的样子。素材用仓库里的 `assets/logo.gif`（240×240、32 帧、0.96s 循环）转出，239 KiB。
- **1.0.2** —— 版本下限修正：`audio_urls` 改为按需传参（AstrBot 4.23.0 才有该参数，旧版没有 `**kwargs`，原来无条件传会直接 `TypeError`，而撤回已先执行——旧消息被删、新回复不来）。同时把 `astrbot_version` 从错误的 `>=4.5.0` 修正为 `>=4.1.0`。
- **1.0.1** —— 多账号修复：撤回时带上消息所属的 `self_id`，一个 aiocqhttp 适配器下挂多个 QQ 号也能正确撤回；消息句柄按账号隔离，撤回无法路由时日志会说明原因。
- **1.0.0** —— 首次发布。

## License

[MIT](LICENSE)
