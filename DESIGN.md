# astrbot_plugin_roll 设计文档

> 状态：**已实现并在本机 AstrBot 4.28.1 + NapCat(aiocqhttp) 上端到端实测通过**
> 目标框架：AstrBot ≥ 4.5.0 / Python 3.10+ · 零第三方依赖 · Windows / Linux / macOS 通用
> （实测环境：AstrBot 4.28.1 + NapCat。最低版本 4.5.0 是逐版本核对源码得出的：该版本起 `event.request_llm()` 支持 `conversation` 参数、`ProviderRequest.conversation` 存在、框架会按本轮消息重写会话历史；4.0.0 起已有插件处理器 `yield` `ProviderRequest` 的管道支持、`should_call_llm`、`plain_result`、`get_client()`、配置注入与 `_conf_schema.json`。）
> 交付位置：本仓库（部署副本：`<AstrBot>/data/plugins/astrbot_plugin_roll`）

---

## 1. 背景与目标

AstrBot 4.28.x 内置指令没有 `/roll`；WebUI 聊天页的「重新生成」只服务于网页端。AstrBot 核心与**所有**平台适配器都**不保留「已发出消息的 message_id」**，也没有任何撤回接口（全仓检索 `delete_msg|recall|delete_message|revoke|unsend` 仅命中 api-key 逻辑）——因此「撤回旧回复」这件事只能由插件按平台自行实现。

目标：用户在对话平台对 bot 的回复不满意时发送 `/roll`，插件基于**同一段上下文**重新生成一条回复，并**尽力**撤回那条旧的平台消息。

非目标：多候选/swipe、重生成更早的历史轮次、WebUI 侧操作、Telegram/Discord 等平台的撤回（v1 只做 QQ）。

---

## 2. 用户可见行为

| 指令 | 说明 |
| --- | --- |
| `/roll` | 重新生成当前会话最后一轮 bot 回复 |
| `/reroll`、`/重roll` | 内置静态别名（注册名是 `roll`，前缀由框架剥离，跟随全局 `wake_prefix`） |

| 场景 | 行为 |
| --- | --- |
| 成功 | **一收到 `/roll` 立即撤回旧的平台消息**（在请求 LLM 之前）→ 用完整主 Agent 管道重新生成 → 新回复送达 |
| 当前无对话 / 无 user 轮 / 内容全空 | `show_tip_when_no_reply=true` 时回复「当前没有可以重新生成的回复。」，否则完全静默 |
| 非管理员（`permission_mode=admin`） | 静默忽略：不提示、不响应，也**不会**让框架去回答字面量 `roll` |
| 冷却中 / 同一会话正在生成 / 会话锁超时（8s） | 静默忽略 |
| 生成失败或没有产出 | 旧消息已在收到 `/roll` 时撤回；框架会发出错误提示，插件另记一条 warning 日志 |
| 撤回被平台拒绝（超时窗、无权限） | 静默降级，只保留新回复，不额外报错 |

---

## 3. 技术方案

### 3.1 为什么复用框架管道（而不是直接调 `ctx.llm_generate`）

处理函数 `yield event.request_llm(...)`，框架的 `ProcessStage`（`pipeline/process_stage/stage.py:38-48`）会接住这个 `ProviderRequest`，走**与普通回复完全相同**的主 Agent 路径：人格、System Prompt、函数工具、知识库、流式、TTS/转图、at/引用装饰、正常回复阶段与历史落库。直接调 `ctx.llm_generate` 会丢掉上述全部能力，并需要自己写历史。

### 3.2 核心流程（`roll/core.py` 的 `RollService.handle`）

```
1. event.should_call_llm(True)            ← 第一件事：禁止框架用字面量 roll 再问一次 LLM
2. 权限（admin / everyone）
3. 在途标记（带 180s 自愈）与冷却时间门
4. 准备（在框架会话锁内）：
     a. 若本会话已有 Agent 在跑 → 静默退出（避免被 follow_up 捕获）
     b. session_lock_manager.acquire_lock(umo)，最多等 8s；超时静默退出
     c. 读会话 → 裁剪历史 → 生成 conversation 副本、抓取待撤回消息 → 释放锁
5. **立即撤回**旧的平台消息（在请求 LLM 之前；仅 QQ）
6. yield event.request_llm(prompt=重建的 prompt, image_urls=…, audio_urls=…, conversation=副本)
7. 生成器恢复后（回复已送达、历史已落库）→ 只做一次诊断：本轮是否真的落库，没有则记 warning
```

**关键点：插件从不写数据库。** 裁剪结果只存在于 `ProviderRequest.conversation` 的**副本**里，由框架在生成成功后统一落库（`internal.py:644-649` 用 `run_context.messages` 覆写）。生成失败时框架什么都不写（`internal.py:587-608`），因此**旧轮次不会丢**。

**为什么撤回放在生成之前**：`yield` 之后才撤回意味着旧消息要一直挂到新回复生成完（实测这次隔了 37 秒），观感很差——用户明确要求「一发送 `/roll` 就先撤回」。代价是显式的：若随后生成失败，旧消息已经不在，只剩框架的错误提示，插件会记一条 warning。第 7 步的诊断之所以只能放在 `yield` 之后，是因为管道是洋葱模型（`scheduler.py:44-74`）：处理器恢复执行时下游阶段（decorate → respond）才刚跑完。顺带一提，该诊断不能依赖 `on_decorating_result` / `after_message_sent` 钩子——它们在流式模式下根本不触发（`result_decorate/stage.py:134-135`、`respond/stage.py:217-231`）。

### 3.3 历史裁剪与 prompt 重建（`roll/history.py`，纯函数）

```
从后往前找最后一条 role == "user" 的条目 i，要求：
  · 不是框架注入的合成消息（MAX_STEPS_REACHED_PROMPT / 工具次数上限中文提示）
  · 其后至少有一条 assistant（否则视为「上一轮没有 bot 回复」→ 提示）
new_history = history[:i]          ← 连同其后的 assistant/tool 调用链整段丢弃
entry[i] 变成 prompt：
  content 是 str  → prompt = content
  content 是 list → text 片段拼接成 prompt；image_url → image_urls；
                    audio_url → audio_urls；think / 未知类型丢弃
```

框架随后会把 `contexts + [user(prompt), assistant(新回复)]` 整体落库，因此最终历史等价于「原用户消息 + 新回复」，`/roll` 这三个字永不进入上下文。注意：其它插件可能在落库前给 `req.prompt` 加上装饰（例如已装的 `astrbot_plugin_llmperception` 会前置「[发送时间: …]」头），所以历史里的用户条目是**同一条消息的装饰版本**，插件不会改写用户内容。

### 3.4 撤回层（`roll/recall.py`）

**捕获**：aiocqhttp 的 `Api.__getattr__` 会把任意未知属性变成 `functools.partial(self.call_action, name)`，而 `CQHttp.send` 最终也走 `send_msg` → `call_action`。因此**只替换平台客户端实例上的一个属性 `bot.call_action`**，就能捕获所有发送动作（`send_group_msg` / `send_private_msg` / `send_msg` / `*_forward_msg`）返回的 `message_id`：

* 包装器是**完全直通**的：先 `await` 原件、返回值原样返回，记录写在裸 `try/except` 里（绝不影响发送）。
* 幂等：重载时从 `bot._roll_orig_call_action` 取真原件再包一层，不叠加。
* 只使用 `vars(bot)` 判断安装状态——因为 `getattr(bot, 任意名字)` 在 CQHttp 上永远不会返回 `None`（`__getattr__` 会伪造一个 partial）。
* 键是 `(platform_id, group|private, str(session_id), self_id)`：两侧都归一化成字符串（发送侧是 int、事件侧是 str），并且只记录**本实例**发出的消息，多实例/多账号同群不会误删别人的消息。第 4 位是**发消息的账号**——aiocqhttp 的 `WebSocketReverseApi.call_action` 在同一个适配器接了多个账号时靠 `self_id` 选择连接，没有它就只能抛 `ApiNotAvailable`，所以句柄必须记住账号并在 `delete_msg` 时带上。账号未知的发送（`bot.send(event)` 兜底路径、主动推送）落在 `self_id=""` 的无账号桶里，仅在精确键为空时才回退查询。
* `terminate()` 还原原件。

**撤回判定**（在 `yield` 之前、即收到 `/roll` 后立即执行）：

```
recall_old 开启
且 进入处理器时抓到了 handle 突发（≤3 条、以最新一条为锚的 2s 窗口，覆盖 TTS 语音+文字）
且 平台 backend 支持撤回（aiocqhttp）
→ 依次调用 bot.call_action("delete_msg", message_id=…, self_id=…)；每一次都单独 try/except，失败仅记日志（`ApiNotAvailable` 额外记一条 warning 说明多账号路由问题）
```

判定之所以能这么简单，是因为此时生成还没发生：只要能走到这里，就说明权限/冷却/在途/会话锁都通过了，而且**确实找到了一轮可重生成的对话**（否则会走提示或静默分支，不撤回）。平台不支持时由 `NullBackend` 直接返回 `False`（一次 debug 日志）。

**生成后的诊断**（`yield` 恢复之后，只记日志、不做撤回）：比较「运行前历史」与「重新读取的历史」——若历史未变化，或末尾的用户轮次并非本次重建的 prompt（考虑压缩改写与其它插件的 prompt 装饰），则记一条 warning，提示本轮可能生成失败。

**平台支持矩阵**：aiocqhttp（QQ：NapCat / Lagrange / LLOneBot / go-cqhttp）= 可撤回；其余平台 = 重生成照常、撤回自动跳过（新增平台只需实现一个 backend）。

### 3.5 并发与限流

* 每会话在途标记（时间戳 + 180s 自愈）+ `cooldown_seconds` 冷却门。
* 框架自身也有按 UMO 的 `session_lock_manager`，但**不能替代**上面的守卫：第二次 `/roll` 的裁剪发生在第一次落库之前。
* 准备阶段的读取放在框架锁内，避免「并发生成中的那一轮还没落库就被本次快照读走」的窗口。

### 3.6 配置（`_conf_schema.json`）

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `permission_mode` | `admin` / `everyone` | `admin` | 非管理员在下拉为 admin 时静默忽略 |
| `recall_old` | bool | `true` | 是否撤回旧回复（仅 QQ 生效） |
| `cooldown_seconds` | int | 3 | 同会话两次 `/roll` 的最小间隔，0 = 不限 |
| `show_tip_when_no_reply` | bool | `true` | 无可重生成内容时是否提示 |

未知的 `permission_mode` 取值会回落到 `admin`（失败方向安全）。

---

## 4. 已知限制与残余风险

1. **并发窗口可能持续数秒**：`/roll` 在框架会话锁内读取历史，但一条并发的消息要等它自己拿到锁之后才会出现在「在途」检测里。若它仍在预处理阶段（例如带图片的消息，可能数秒），`/roll` 会读到尚未包含它的历史，随后框架以本次上下文覆写历史，那一轮并发消息可能被覆盖（历史不会损坏，但会少一轮）。单人连续操作基本不会触发。
2. **撤回目标是「本实例在该会话发出的最后一条消息」**：捕获层记录本实例所有出站发送，包括主动推送/cron 推送。若 `/roll` 之前恰好有一条主动推送，被撤回的是那条推送而非想替换的回复——收到回复后立即 `/roll` 即可避免歧义；它不会删到其它 bot/其它实例的消息。
3. **撤回先于生成**：这是用户明确要求的观感取舍——若随后生成失败，旧消息已经不在，只剩框架的错误提示（日志会有 warning）。
4. **分段回复**：一次回复被拆成多条时，最多撤回同一个 2 秒突发内的 3 条；异常长的多段回复可能残留更早的段。
5. **撤回是尽力而为**：平台拒绝（超出撤回时限、群内权限不足）时静默跳过；`/roll` 与聊天本身不受影响。
6. **事后诊断的边界**：`yield` 之后只做日志诊断；判定用「末尾用户轮次 + 文本包含」，因此超短消息（1–2 字）可能漏报一次 warning，合成提示词造成的异常尾部可能误报一次 warning。不影响撤回与聊天。
7. **事件被其它插件 `stop_event()`**：调度器会中断，`yield` 之后的诊断日志不会执行（不影响撤回，因为撤回在生成前已完成）。
8. **运行器前提**：`agent_runner.runner_type` 必须是 `local`（AstrBot 默认）；否则启动日志给出警告，历史写入由第三方运行器自行管理。
9. **平台**：Telegram / Discord 等平台 v1 不实现撤回。
10. **合成消息过滤**：框架注入的 `role=user` 提示词按已导入的常量与镜像的中文字面量过滤；若未来版本改动文案，理论上可能被当成用户的最后一轮——但由于插件不写库，其影响仅限那一次重生成。
11. **账号未知的发送**：框架的 `bot.send(event)` 兜底路径与主动推送不携带 `self_id`，这类消息落在无账号桶里。单账号下照常可撤回；多账号下 `delete_msg` 无法路由（记一条 warning，消息保留）。

---

## 5. 验证

### 5.1 离线单测（不 import AstrBot，任意 OS 可跑）

```bash
python -m pytest tests -q          # 85 passed
python -m compileall -q main.py roll tests
```

覆盖：裁剪/prompt 重建表驱动用例（字符串、多模态、think、工具调用尾部、合成消息、无 assistant、全空、首条即 user、非列表历史）、权限矩阵、入口必设 `call_llm`、静默/提示分支、会话锁的获取顺序与真实超时、**撤回必须发生在请求发出之前**的顺序断言、并发消息导致的「未确认本轮」诊断、TTS 突发、键类型归一化、aiocqhttp 包装器（5 个发送动作、非发送动作忽略、无 message_id 忽略、异常透传、返回值直通、幂等安装与还原、`__getattr__` 陷阱）、以及「插件绝不调用 `update_conversation`」的不变量。

### 5.2 真实对象与真机实测

* 真实 `Conversation`（dataclass）/ `ProviderRequest` / `aiocqhttp.CQHttp` 实例上的包装器安装、捕获、删除与还原均已验证。
* 真实会话历史（108 条）裁剪出 106 条前缀 + 2407 字符 prompt，后接恰好一条 assistant。
* **真机（AstrBot 4.28.1 + NapCat）第一轮**（当时是「生成后再撤回」的修订版）：15:11 用户消息 → 15:11 旧回复 → 15:12:58 `/roll` → 15:13:39 新回复；数据库核对显示历史中旧回复已被替换、新回复就位、`/roll` 未出现在任何会话的任何条目中；用户在 QQ 侧确认旧消息**被成功撤回**。
* **真机（本轮最终修订版，撤回先于生成）**：`data/logs/astrbot.log` 中
  `16:11:21.385 Plugin astrbot_plugin_roll (1.0.0) by solitude: …`（重载）→
  `16:13:09.055 plugin -> astrbot_plugin_roll - roll` →
  `16:13:09.131 /roll on …: regenerating with 94 context entries, 1 recall candidate(s)` →
  `16:13:09.315 recall on …: 1 withdrawn, 0 failed`；
  NapCat 容器日志同一时间窗：`16:13:04 旧回复` → `16:13:09 收到 /roll` → `16:13:50 新回复`。
  即**撤回发生在收到指令后 0.26 秒、比新回复早 41 秒**；日志行号 `roll.core:416` 只存在于本修订版，可据此确认跑的就是被评审的代码。此外 `94 context entries`（此前为 110）说明框架的 `truncate_by_turns` 压缩已在生效——也就是过去会让撤回静默失效的那个条件。
* **实测覆盖范围**：真机只覆盖了「私聊 + admin 模式 + 成功路径 + 可撤回平台」。提示分支、非管理员静默、冷却/在途静默、不支持撤回的平台均只有离线单测覆盖（见 5.1），未在真机上逐一走过。
* **多账号修复的实测（1.0.1）**：真实 `aiocqhttp.CQHttp` + 真实 `WebSocketReverseApi`（两个会回应 `echo` 的模拟 OneBot 连接）下，账号 10001 的私聊回复落在键 `(platform_id, private, '456', '10001')`，`delete_msg` 精确送到 10001 的连接上；同样的调用在修复前抛 `ApiNotAvailable`。离线套件 85 passed（新增 8 项：按账号捕获、账号隔离、无账号回退、路由失败只 warning 一次）。

### 5.3 评审

* 方案可行性门禁：3 轮独立对抗评审（不同模型代际），累计 15 项发现全部核实并修入实现；三轮均未取得 PASS，随后取得用户授权在**如实披露**的前提下继续实现（见交付报告）。
* 最终质量门禁第 1 轮：发现 3 项（1 blocker = 真机证据绑定在旧版本上、2 must-fix = 撤回门禁在压缩改写前缀后会静默失效 / 本文档曾经过期），已修复并补测。
* 最终质量门禁第 2 轮：独立复核确认「运行时满足 A1–A7」（自行比对 6 个文件哈希、复跑测试、用日志独立验证 A7、用数据库独立验证 A2），但仍判 FAIL——原因是改时序后**文档与代码不一致**（`roll/core.py` 模块 docstring、README 设计要点、本文档 §5.2）以及两条未披露的行为边界（主动推送也可能被撤回、并发窗口可能持续数秒）。已全部修正。
* 最终质量门禁第 3 轮：确认上述文档修正完成、运行时无功能缺陷，但发现一个 **P0 打包问题**——插件目录里混入了 `data/`（内含 AstrBot 生成的 `cmd_config.json` 与 `t2i_templates/`）：根因是**验证脚本自己**在插件目录内 `import astrbot`，于是 AstrBot 按 cwd 建了 `data/`。已删除两份、加入 `.gitignore`、README 增加警告，并改用 `/MIR` 重新部署。
* 最终质量门禁确认轮：P0 与「撤回后未遗忘 handle」均已确认修复，另发现 `_last_at` 清理**在 `cooldown_seconds=0` 时不可达**（清理调用点位于 `_is_cooling_down` 的提前返回之后），已把清理移到 `handle()` 入口无条件执行并补测。
* 最终质量门禁窄确认轮：**VERDICT: PASS**（评审自行做了变异测试——把清理函数替换为空实现后测试确实失败，证明用例非空洞）。


---

## 6. 与最初设计的差异

| 最初设计 | 最终实现 | 原因 |
| --- | --- | --- |
| `ctx.llm_generate(...)` 直接请求 provider | `yield event.request_llm(...)` | 复用完整管道（人格/工具/知识库/装饰/落库） |
| 先把裁剪结果写回数据库 | 只放 `conversation` 副本，绝不写库 | 生成失败时旧轮次不能丢 |
| 用 `get_friend_msg_history` / `get_group_msg_history` 反查旧消息 id | 包装 `bot.call_action` 捕获发送回执 | 不依赖各 OneBot 实现的非标准接口 |
| 配置项 `aliases` | 内置静态别名 | 同一 handler 上多个指令装饰器是 AND 语义，动态别名不可靠 |
| 撤回时机未定义 | 收到 `/roll` 立即撤回，然后才生成 | 用户反馈：等新回复期间旧消息一直挂着观感差 |
