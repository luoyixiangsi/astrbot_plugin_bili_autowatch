# astrbot_plugin_bili_autowatch

让 AstrBot 自己刷 B 站视频，并按概率自动点赞、投币、收藏、发评论，
看完后还能让大模型写一段观后感，主动私聊发给你。

## 它做了什么

模拟网页端真实的观看流程，而不是简单 ping 一个接口：

1. `x/click-interface/click/web/h5` —— 上报点击（可选，默认关，该接口有风控）
2. `x/click-interface/web/heartbeat` —— 播放心跳，每 12~18 秒一次，带 `played_time` / `realtime` / `session` 等字段
3. `x/v2/history/report` —— 上报观看进度（可选）

看完之后按配置的概率触发互动，评论可以由大模型根据视频标题与简介生成；
观后感则由大模型根据标题、简介、观看时长和实际做的动作生成，推送到指定 QQ。

## 安装

1. 把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_bili_autowatch/`
2. 重启 AstrBot，或在 WebUI 插件页点「重载插件」
3. 打开插件配置，填入 Cookie

依赖 `bilibili-api-python`，AstrBot 会按 `requirements.txt` 自动安装。
另外它需要一个 HTTP 客户端，AstrBot 自带的 `aiohttp` / `httpx` 已经够用。

## 配置 Cookie

浏览器登录 B 站后打开开发者工具 → Application → Cookies → `https://www.bilibili.com`，复制：

- `SESSDATA`（必需）
- `bili_jct`（必需，写操作要用它做 CSRF 校验）
- `buvid3`（建议填，能显著降低被风控的概率）
- `DedeUserID`（可选，填了心跳里的 `mid` 才准确）

填完用 `/bili check` 验证一下。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/bili` | 查看帮助 |
| `/bili start` | 开始后台自动刷 |
| `/bili stop` | 停止 |
| `/bili status` | 查看运行状态 |
| `/bili once` | 立刻刷一个视频 |
| `/bili video BV1xx` | 指定视频刷一次 |
| `/bili review [BV1xx]` | 刷完一个视频并把观后感发到当前会话 |
| `/bili comment BV1xx` | 给指定视频发一条评论 |
| `/bili check` | 校验 Cookie |
| `/bili here` | 把结果推送到当前会话 |
| `/bili reset` | 清空观看记录与每日计数 |

默认仅管理员可用，可在配置里关掉。

## 关键配置

- `dry_run`：演练模式，只挑视频不写请求。**第一次建议先开这个验证配置。**
- `source`：`hot` / `rank` / `recommend` / `search` / `favorite` / `up`
- `watch.max_watch_seconds`：单个视频最长看多久（默认 180 秒），避免长视频卡住
- `schedule.daily_limit` / `interval_min` / `interval_max` / `quiet_hours`：频率与静默时段
- `interact.*`：点赞、投币、收藏的概率
- `comment.enabled` / `probability` / `source`：评论开关与来源（`llm` 或 `template`）
- `review.enabled` / `provider_id` / `persona` / `max_length`：观后感开关与风格
- `notify_qq`：观后感发给哪个 QQ（填纯数字），留空则回退到 `notify_session`
- `notify_push_limit`：每天最多推送几条结果，`0`（默认）表示不限制

## 观后感推送到 QQ（NapCat）

后台每刷完一个视频，就会把「标题 / UP主 / 时长 / 动作 / 观后感」整段结果推送出去。
推送目标按下面的顺序找第一个能用的：

1. `notify_session`（用 `/bili here` 可以一键填入当前会话）
2. `notify_qq`，填纯数字 QQ 号，插件会自动补成 `default:FriendMessage:<QQ>`
3. AstrBot 管理员列表里形如 `default:FriendMessage:123456` 的会话

`default` 要和 NapCat 平台适配器的 `id` 一致（AstrBot 里默认就是 `default`）。
最省事的做法：直接用要接收观后感的 QQ 给机器人发一条私聊，然后

```
/bili here
```

把当前私聊设为播报目标，之后每次刷完就会自动发到这里。

观后感默认开启，用的是 AstrBot 当前的默认对话模型；也可以在
`review.provider_id` 里单独指定一个提供商。演练模式下不会调用模型。

推送条数默认不设上限。如果想少收点，把 `notify_push_limit` 设成正整数即可，
超过之后当天的后台推送会自动停下（手动 `/bili review` 不受这个上限限制）。
当天已推送条数可以用 `/bili status` 查看，`/bili reset` 会把计数一起清零。

## 数据存放

观看记录、每日计数、历史日志都写在
`AstrBot/data/plugin_data/astrbot_plugin_bili_autowatch/` 下，插件更新不会丢。

## 实测说明

- 心跳接口（`x/click-interface/web/heartbeat`）实测返回 `code: 0`，参数结构被 B 站接受。
  按官方 API 文档，该接口「亦可记录播放历史」，是刷视频的主力接口。
- 观看进度接口（`x/v2/history/report`）在测试中即使换成有效格式的会话也返回
  `-400 请求错误`，加 WBI 签名、换 Header、改 GET 都无效。插件已把它做成
  「失败不影响流程」：接口不可用时只记一条 info 日志，观看照常继续。
- 播放量上报接口有 200 次/天的限制，默认关闭，不要滥用。

## 风险提示

这类行为可能违反 B 站用户协议，存在账号被限流或封禁的风险。
请控制频率、不要开高并发，风险自负。
