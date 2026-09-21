<div align="center">

<img src="assets/logo.gif" width="150" alt="astrbot_plugin_roll">

# astrbot_plugin_roll

给 bot 的回复配一个「重新生成」。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.5.0-orange?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

发一条 `/roll`，bot 上一条回复就被换掉。

```text
你   /roll
bot  （旧回复被撤回）
bot  唔，换个说法……我觉得还是先去翻一眼日志比较好。
```

新回复接着上一轮说，人格、system prompt、记忆、工具、知识库、流式、转图/TTS 都跟平时一样，只是换了个说法。QQ 上旧消息会顺手撤回，其它平台留在那儿。

## 为什么需要它

WebUI 的聊天页里，每条回复旁边都有个「重新生成」，不满意点一下就重来。到了 QQ、Telegram 这些地方就没这个按钮了：bot 回错一句话，你要么再打一遍问题求它换个说法，要么就这么算了。

再打一遍的代价不只是多费一条消息。那句话会进上下文，历史里凭空多出一轮，后面每次对话都拖着它。

`/roll` 就是把网页上那个按钮搬进聊天框。旧的撤掉，同一个问题重新答一遍，看起来就像刚才那句本来就是这个版本。

## 安装

插件市场里搜 `roll` 装。手动装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/insolitude610/astrbot_plugin_roll_message.git astrbot_plugin_roll
```

目录名留成 `astrbot_plugin_roll`，然后到 WebUI 插件页点一下「重载插件」。不用装任何依赖。

## 使用

| 指令 | 说明 |
| --- | --- |
| `/roll` | 重新生成当前会话最后一条 bot 回复 |
| `/reroll`、`/重roll` | 别名，效果一样 |

前缀跟着 AstrBot 的全局 `wake_prefix` 走。全局是 `/` 就是 `/roll`，改成 `!` 就是 `!roll`。

一个典型回合：

1. bot 回了一句不太满意的，比如把日志里的报错说反了；
2. 你发 `/roll`，旧消息当场消失；
3. 过一会儿新回复到，还是同一个问题，换了种说法；
4. 还不满意就再 `roll` 一次。

默认只有管理员（`admins_id` 里的那些人）能用，其他人发 `/roll` 没有任何反应，bot 也不会把「roll」当成一句话来回答。想让群里谁都能用，把 `permission_mode` 改成 `everyone`。改之前先想想 token，每次 `/roll` 都是一次真实的模型调用。

## 配置

都在 WebUI 的插件配置页里：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `permission_mode` | `admin` | `admin` 只有管理员能用；`everyone` 谁都能用。每次 `/roll` 都是一次真实的模型调用 |
| `recall_old` | `true` | 收到 `/roll` 就把旧消息撤回。关掉的话旧回复留着，只发一条新的 |
| `cooldown_seconds` | `3` | 同一个会话两次 `/roll` 的最小间隔，`0` 表示不限 |
| `show_tip_when_no_reply` | `true` | 没有可重新生成的回复时回一句提示。关掉就完全静默 |

## 平台支持

| 平台 | 重新生成 | 撤回旧消息 |
| --- | --- | --- |
| QQ（NapCat / Lagrange / LLOneBot / go-cqhttp） | ✅ | ✅ |
| Telegram、Discord、Slack、飞书、KOOK、企业微信、钉钉、QQ 官方、WebChat 等 | ✅ | 跳过 |

撤回得调平台自己的接口，AstrBot 和各个适配器都没留这个口子，只能一个平台一个平台补。目前只有 QQ 补了，别的平台照常重新生成，旧消息不动，也不报错。

## 几点说明

- **旧的先撤，然后才去生成。** 所以发完 `/roll` 旧消息是立刻没的，不用干等新回复（实测那次等了 41 秒，挂着确实难受）。万一这次生成失败，旧消息已经撤了，你只会看到一条报错。
- **撤回是尽力而为。** 超出 QQ 的撤回时限、群里权限不够，那就旧消息留着，不报错，也不影响后面的聊天。
- **撤的是这个会话里 bot 发的最后一条消息。** 如果 `/roll` 之前 bot 正好主动推过一条（定时任务或者主动消息插件），撤掉的会是那条。收到回复马上 `/roll` 就不会有歧义。
- **生成期间再发 `/roll` 没反应**，不会并发去调模型。
- **回复被拆成多条的时候**，最多撤掉 2 秒内的 3 条，特别长的分段回复可能留个尾巴。
- **`/roll` 不进对话历史。** 历史里存的是「你原来那条消息 + 新回复」，所以它既不会被 bot 当成一句话回答，也不会污染后面的上下文。

## 排障

先在 AstrBot 日志里搜 `astrbot_plugin_roll`，插件每步都会带这个前缀。

| 现象 | 原因与处理 |
| --- | --- |
| 发 `/roll` 完全没反应 | 多半是权限。默认只有管理员能用，非管理员静默忽略；换成管理员账号，或把 `permission_mode` 设成 `everyone` |
| 有反应，但旧消息还在 | 确认在 QQ 上、`recall_old` 是开着的；也可能超出撤回时限或群里权限不够，这种就是撤不掉 |
| 回一句「当前没有可以重新生成的回复。」 | 这个会话还没有 bot 回复可换，新会话或者刚重置过都会这样。不想看到这句就把 `show_tip_when_no_reply` 关掉 |
| 连着发两条只有一条生效 | 冷却时间（默认 3 秒），或者上一条还在生成 |
| 明明刚聊过却说没有可重生成的回复 | 先看 `agent_runner.runner_type` 是不是 `local`，第三方执行器不保证准 |
| 撤回之后只收到一条报错 | 模型调用失败了，旧的已经撤掉，日志里会有 warning |
| 日志里搜不到 `astrbot_plugin_roll` | 插件没被触发。确认唤醒前缀对不对，插件是不是启用状态 |
| 想知道撤回为什么失败 | 具体原因记在 `DEBUG` 级别，把 AstrBot 的 `log_level` 调到 `DEBUG` 再看 |

## 版本要求

- 需要 **AstrBot 4.5.0 或更高**。`/roll` 靠的两个框架能力是 4.5.0 才补齐的：把裁剪过的历史交给模型，以及让框架按本轮消息重写会话历史。版本低了请先升级，否则 `roll` 出来的东西不作数。
- Python 3.10 以上，不用装依赖，Windows / macOS / Linux 都行。
- Agent 执行器保持默认的 `local`。换成第三方的，历史由它自己管，`/roll` 就不一定准了，启动时也会收到一条 warning。
- 开发和实测环境是 AstrBot 4.28.1 + NapCat，私聊真机跑过，包括 TTS 语音+文字双输出和历史压缩的场景。

## 更新日志

- **1.0.0** —— 首次发布。

## License

[MIT](LICENSE)
