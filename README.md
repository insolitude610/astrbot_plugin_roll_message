# astrbot_plugin_roll

对 bot 的回复不满意？发一个 **`/roll`**，让它重写一条 —— 旧的那条会立刻消失。

```
你:  /roll
bot: (旧回复被撤回)
bot: (重新生成的新回复)
```

## 特点

- **真的是重新生成**：走的是和普通回复完全同一条链路（人格、System Prompt、工具、知识库、流式输出、转图/TTS 都在），所以新回复的语气和风格跟平时一致，只是换了个说法。
- **旧消息立刻撤回**：按下 `/roll` 的瞬间旧消息就没了，不用干等新回复（QQ / aiocqhttp 支持；其它平台自动跳过，不报错）。
- **权限可控**：默认只有 AstrBot 管理员能用，也可以放开给所有人。
- **不污染上下文**：`/roll` 这两个字永远不会进对话历史，也不会让 bot 去"回答"/roll 本身。

## 安装

1. 把整个 `astrbot_plugin_roll` 目录放进 AstrBot 的 `data/plugins/` 下。
2. 在 WebUI 插件页点「重载插件」。
3. 在聊天里发 `/roll` 试试。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/roll` | 重新生成当前会话最后一轮 bot 回复 |
| `/reroll`、`/重roll` | 内置别名 |

前缀跟随 AstrBot 全局 `wake_prefix`：全局是 `/` 就是 `/roll`，改成 `!` 就是 `!roll`。

## 配置

在 WebUI 的插件配置页里改：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `permission_mode` | `admin` | `admin` = 只有管理员能用，其他人发 `/roll` 会被静默忽略；`everyone` = 任何人都能用（每次调用都消耗 token，群聊慎用） |
| `recall_old` | 开 | 是否撤回被替换掉的旧消息 |
| `cooldown_seconds` | `3` | 同一会话两次 `/roll` 的最小间隔，`0` = 不限 |
| `show_tip_when_no_reply` | 开 | 没有可重新生成的回复时，要不要回复一句提示 |

## 平台支持

| | 重新生成 | 撤回旧消息 |
| --- | --- | --- |
| QQ（aiocqhttp：NapCat / Lagrange / LLOneBot / go-cqhttp） | ✅ | ✅ |
| Telegram / Discord / Slack / Lark / KOOK / 企业微信 / 钉钉 / QQ 官方 / WebChat | ✅ | 自动跳过 |

撤回需要平台自己的接口：AstrBot 框架本身没有这个能力，所以目前只实现了 QQ 侧。其它平台要接入只需补一个 backend，重生成逻辑不用动。

## 行为说明

- **撤回发生在生成之前**：所以你按下 `/roll` 后旧消息立刻消失，新回复随后才出现。代价是如果这次生成失败，旧消息已经不在，只会看到框架的报错。
- **撤回失败会静默降级**：超出 QQ 的撤回时限、群里没权限等情况下，只是旧消息留着，其它不受影响。
- **连续快速 `/roll`**：生成期间重复发送会被忽略，避免重复调用模型。
- **撤回目标是"该会话里 bot 发的最后一条消息"**：如果刚好在 `/roll` 之前 bot 主动推过一条消息，被撤回的会是那条。收到回复后马上 `/roll` 就不会有歧义。

## 兼容性

- 需要 **AstrBot ≥ 4.5.0**（该版本起 `event.request_llm()` 才支持 `conversation` 参数、框架才会按本轮消息重写对话历史 —— 这两个是本插件的核心依赖）。更早的版本请先升级。
- 开发与实测环境：**AstrBot 4.28.1 + NapCat**（QQ 私聊，含 TTS 双输出与历史压缩场景）。
- Python ≥ 3.10，**零第三方依赖**，Windows / macOS / Linux 通用。

## 开发

业务逻辑都在 `roll/` 里，不 import 任何 AstrBot 模块，可以脱离 AstrBot 直接测：

```bash
python -m pytest tests -q      # 77 个用例
python -m compileall -q main.py roll
```

| 文件 | 职责 |
| --- | --- |
| `roll/history.py` | 纯函数：找出要重生成的轮次、裁剪上下文、重建 prompt |
| `roll/recall.py` | 撤回层：捕获 bot 发出的消息 id、调用平台删除接口 |
| `roll/core.py` | 主流程：权限、冷却、会话锁、发起重生成、撤回 |
| `main.py` | AstrBot 接线 |

> 调试小贴士：不要在插件目录里 `import astrbot`（例如做导入冒烟测试）——AstrBot 会按当前目录生成一个 `data/`，里面是会带凭据字段的 `cmd_config.json`。要测就换个临时目录跑。

## License

[MIT](LICENSE)
