<div align="center">

<img src="assets/logo.gif" width="150" alt="astrbot_plugin_roll">

# astrbot_plugin_roll

给 bot 的回复配一个「重新生成」。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.5.0-orange?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

AstrBot 的 WebUI 里，每条回复旁边都有个「重新生成」，不满意就点一下。这个插件把同样的东西搬到了聊天平台：私聊或者群里发一条 `/roll`，bot 刚才那条回复就会被换掉。

```text
你   /roll
bot  （旧回复被撤回）
bot  唔，换个说法……我觉得还是先去翻一眼日志比较好。
```

新回复不是插件另起炉灶编的，而是原样走了一遍平时的回复流程，人格、System Prompt、函数工具、知识库、流式输出、转图/TTS 一个都没少。所以语气跟你平时看到的一致，只是换了个说法。

QQ（aiocqhttp）下旧的那条平台消息会被顺手撤回。框架本身没有撤回接口，这部分是插件按平台自己补的，目前只补了 QQ；其它平台就只是重新生成，旧消息留在那儿。

整个插件就干这一件事，代码量不大，`roll/` 里的逻辑不依赖 AstrBot，能单独测。

## 安装

插件市场里搜 `roll` 直接装。手动装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/insolitude610/Astrbot_plugin_roll_message.git astrbot_plugin_roll
```

目录名留成 `astrbot_plugin_roll`，然后到 WebUI 插件页点一下「重载插件」。

## 用法

| 指令 | 说明 |
| --- | --- |
| `/roll` | 重新生成当前会话最后一条 bot 回复 |
| `/reroll`、`/重roll` | 别名，效果一样 |

前缀跟着 AstrBot 的全局 `wake_prefix` 走。全局是 `/` 就是 `/roll`，改成 `!` 就是 `!roll`。

默认只有 AstrBot 管理员（`admins_id` 里的那些人）能用。其他人发 `/roll` 会被静默忽略：不提示、不响应，也不会让 bot 去回答「roll」这两个字本身。想让群里谁都能用，把 `permission_mode` 改成 `everyone`，改之前先想想 token。

## 配置

都在 WebUI 的插件配置页里：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `permission_mode` | `admin` | `admin` = 仅管理员可用；`everyone` = 谁都能用。每次 `/roll` 都是一次真实的模型调用 |
| `recall_old` | 开 | 是否撤回被替换掉的旧消息。只有 QQ 会真的撤，其它平台自动跳过 |
| `cooldown_seconds` | `3` | 同一会话两次 `/roll` 的最小间隔，`0` 表示不限 |
| `show_tip_when_no_reply` | 开 | 没有可重新生成的回复时，是否回一句提示。关掉就完全静默 |

## 平台支持

| 平台 | 重新生成 | 撤回旧消息 |
| --- | --- | --- |
| QQ（aiocqhttp：NapCat / Lagrange / LLOneBot / go-cqhttp） | ✅ | ✅ |
| Telegram / Discord / Slack / 飞书 / KOOK / 企业微信 / 钉钉 / QQ 官方 / WebChat … | ✅ | 自动跳过 |

撤回得调平台自己的接口，而 AstrBot 核心和所有适配器都没有保留「已发出消息的 message_id」，所以这块只能一个平台一个平台地补。想加新平台的话，实现一个 backend 就行，重生成那部分不用动。

## 几个坑，先说在前面

- **撤回发生在生成之前。** 收到 `/roll` 就先撤，然后才去请求模型，所以旧消息是立刻消失的，不用干等新回复（实测那次等了 41 秒，挂着确实难受）。代价也很直接：万一这次生成失败，旧消息已经没了，你只会看到框架的报错。
- **撤回是尽力而为。** 超出 QQ 的撤回时限、群里权限不够，这些情况就只是旧消息留着，不报错，也不影响聊天。
- **撤的是「这个会话里 bot 发的最后一条消息」。** 如果恰好在 `/roll` 之前 bot 主动推过一条（定时任务、主动消息插件之类），被撤的会是那条。收到回复之后马上 `/roll` 就不会有歧义。
- **生成期间重复发 `/roll` 会被忽略**，不会并发去调模型。
- **回复被拆成多条发出去时**，最多能撤掉同一个 2 秒突发内的 3 条，特别长的多段回复可能有残留。
- **`/roll` 不会进对话历史**，也不会被 bot 当成一句话来回答。历史里存的是「你原来那条消息 + 新回复」，跟重新生成过的效果一致。

## 兼容性

- 需要 **AstrBot ≥ 4.5.0**。4.5.0 起 `event.request_llm()` 才支持 `conversation` 参数，框架也才会按本轮消息重写会话历史，这两件事都是本插件的前提，更早的版本请先升级。
- 开发和实测环境是 AstrBot 4.28.1 + NapCat，私聊真机跑过，包括 TTS 双输出和历史压缩的场景。
- Python ≥ 3.10，零第三方依赖，Windows / macOS / Linux 通用。
- Agent 执行器得是默认的 `local`。换成第三方执行器时启动会打一条 warning，历史由那个执行器自己管，`/roll` 就不一定准了。

## 开发

业务逻辑都在 `roll/` 里，不 import 任何 AstrBot 模块（跟框架打交道的那部分全在 `main.py`），可以脱离 AstrBot 直接跑：

| 文件 | 干什么的 |
| --- | --- |
| `roll/history.py` | 纯函数：找出该重生成的轮次、裁剪上下文、重建 prompt |
| `roll/recall.py` | 撤回层：捕获 bot 发出的消息 id、调用平台的删除接口 |
| `roll/core.py` | 主流程：权限、冷却、会话锁、发起重生成、撤回 |
| `main.py` | AstrBot 接线 |

改完代码建议跑一下语法检查：

```bash
python -m compileall -q main.py roll
```

本地另外维护了一套 77 个用例的离线单测（不 import AstrBot，任意系统都能跑），不过测试目录没有随仓库发布，这样插件包能瘦一点。

> 一个小提醒：别在插件目录里 `import astrbot`（比如随手写个导入冒烟测试）。AstrBot 会按当前目录建一个 `data/`，里面是会带凭据字段的 `cmd_config.json`。要测就换个临时目录。

## 更新日志

- **1.0.0** —— 首次发布。

## License

[MIT](LICENSE)
