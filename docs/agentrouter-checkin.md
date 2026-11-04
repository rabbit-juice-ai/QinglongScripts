# AgentRouter-Checkin 使用文档

一个运行在青龙面板中的单文件 Python 定时签到脚本。无需数据库、无 Web 服务、无第三方依赖（仅 Python 标准库）。

- 脚本路径：`tasks/agentrouter-checkin/agentrouter_checkin.py`
- 测试：`tasks/agentrouter-checkin/tests/mock_test.py`（本地 Mock，不联网）
- 站点：<https://agentrouter.org/>

## 1. 功能概览

- 从青龙环境变量读取任意数量账号（JSON 数组，动态 1～20 个）；
- 按配置顺序串行处理每个启用账号；
- 每个账号独立 Cookie 会话（按 username 保存），登录前尝试读取签到前余额；
- 使用网站用户名/邮箱 + 密码登录，登录可能触发当日签到（具体行为以站点为准）；
- 登录后读取最新余额，计算本次运行内余额变化；
- 单个账号失败不影响其他账号；
- 全部结束后发送一条飞书汇总（优先青龙 `notify.py`，回退 Webhook）；
- 遇到 Turnstile / 验证码 / MFA 不绕过，暂停该账号并记录原因。

## 2. 环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.9+（青龙自带 python3 即可） |
| 第三方依赖 | 无（仅标准库：urllib、http.cookiejar、fcntl、json 等） |
| 网络 | 能访问 `agentrouter.org` 与飞书（建议全部经 Mihomo 代理） |
| 权限 | 脚本需要写 `AGENTROUTER_DATA_DIR`（默认 `/ql/data/agentrouter-checkin`） |

无需 `pip install`，无需 venv，无需 Chromium/Playwright。

## 3. 青龙环境变量

在青龙面板「环境变量」页添加：

| 变量 | 必填 | 说明 |
|---|---|---|
| `AGENTROUTER_ACCOUNTS` | 是 | 账号 JSON 数组，见下方示例 |
| `PROXY_URL` | 生产必填 | 例如 `http://192.168.31.29:37890` |
| `AGENTROUTER_WEBHOOK_URL` | 可选 | 飞书自定义机器人 Webhook；未配置时仅用青龙 notify.py |
| `AGENTROUTER_DATA_DIR` | 可选 | 运行时数据目录，默认 `/ql/data/agentrouter-checkin` |
| `AGENTROUTER_BASE_URL` | 可选 | 默认 `https://agentrouter.org`，一般不用改 |

### 账号配置示例

```json
[
  {
    "id": "account_a",
    "username": "你的登录用户名或邮箱",
    "password": "你的密码",
    "enabled": true,
    "expected_name": "网站上的用户名（可选）"
  },
  {
    "id": "account_b",
    "username": "另一个账号",
    "password": "另一个密码",
    "enabled": true
  }
]
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 本地唯一标识，`^[a-z0-9][a-z0-9_-]{0,63}$`，用于日志与锁 |
| `username` | 是 | 登录用用户名或邮箱 |
| `password` | 是 | 登录密码 |
| `enabled` | 是 | `true` / `false`；禁用账号不执行 |
| `expected_name` | 否 | 配置后启用严格身份对照：登录后以 `GET /api/user/self` 为准，比对返回的 `username` 或 `email`；不一致则该账号失败、余额不采用。不配置则“登录成功即成功”，名称差异仅提示 |

校验规则：JSON 必须合法；数量 1～20；id 唯一；用户名/密码非空；未知字段报错。任何错误都会列出全部问题并以退出码 2 结束，不启动登录。

## 4. 青龙任务配置

任务命令：

```text
cd /ql/data/scripts/<你的脚本目录> && python3 agentrouter_checkin.py --mode full
```

建议：

- 任务超时 ≥ 35 分钟；
- 如青龙支持“不允许并发”，请开启（脚本另有全局文件锁兜底）；
- 首次部署先跑 `--mode dry-run` 与 `--mode site-connectivity`，再跑单账号，最后再配 Cron；
- Cron 时间建议每天一次，避开你尚未验证的站点重置边界。

## 5. 运行模式

| 模式 | 命令 | 说明 |
|---|---|---|
| 全量 | `--mode full` | 按顺序执行全部启用账号 |
| 单账号 | `--mode single --account <id>` | 只执行指定账号（必须存在且启用） |
| 配置校验 | `--mode dry-run` | 仅解析配置并打印待执行列表，不登录 |
| 站点连通 | `--mode site-connectivity` | 仅请求 `/api/status`，不登录 |
| 飞书连通 | `--mode feishu-connectivity` | 发送一条测试消息，不访问站点 |
| 会话诊断 | `--mode session-check --account <id>` | 仅用已保存 Cookie 查询 `/api/user/self`，不登录；分别测试带/不带 `New-API-User` 头 |

## 6. 执行流程

1. 获取单一全局锁；锁冲突输出 `TASK_NOT_EXECUTED`，退出码 3，不执行账号、不发送通知；
2. 读取并校验 `AGENTROUTER_ACCOUNTS`；
3. 逐个处理启用账号：
   - 加载该账号会话（`data/sessions/<username>_<hash>.cookies` + `.json` 中的 user_id）；
   - 若会话有效：读取签到前余额 → 退出旧会话 → 确认退出；
   - 若会话无效/过期：签到前余额记为“未知”，直接登录；
   - 检查站点是否启用 Turnstile；启用则暂停该账号（不绕过）；
   - 用配置的用户名/密码登录，记录 `checked_in` 原始值；
   - 登录后重新获取用户信息，核对身份（配置了 `expected_name` 时严格比对）；
   - 读取签到后余额，计算变化（仅前后均成功时）；
4. 打印每个账号结果与汇总到青龙任务日志；
5. 发送一条飞书汇总；发送结果不确定时只记录日志，不自动重发。

## 7. 结果与通知

- 日志与推送中的账号标题使用站点返回的 username（失败时退回配置 username）；
- 余额按 `/api/status` 的 `quota_per_unit` 换算为美元显示，并保留原始 quota，例如 `$499.98（249988608 quota）`；
- 余额未知时显示“未知”，绝不显示为 0；
- `checked_in` 只记录原始值：`true` 显示“含义待验证”，`false` 显示“不解释为已签到”；
- 通知顺序：青龙 `notify.py` 明确成功即结束；明确失败回退 `AGENTROUTER_WEBHOOK_URL`；结果不确定记录 `FEISHU_AMBIGUOUS` 且不自动回退；Webhook 结果不确定记录日志且不重试；
- 通知失败不影响签到结果。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 全部启用账号成功 |
| 1 | 部分账号失败/暂停/未执行 |
| 2 | 全部失败，或配置错误 |
| 3 | 锁冲突（`TASK_NOT_EXECUTED`） |

## 8. 数据与安全

运行时目录（默认 `/ql/data/agentrouter-checkin`）：

```text
data/
├── sessions/
│   ├── <username>_<hash>.cookies   # 每账号会话 Cookie（0600）
│   └── <username>_<hash>.json      # 会话附属信息：user_id（0600）
└── locks/
    └── global.lock                 # 单一全局锁
```

- 无数据库、无签到/余额历史持久化；
- 会话文件按 username 命名，包含有效 Cookie 与 user_id，视同凭据，请勿分享或提交 Git；
- 删除账号配置不会删除会话文件；确认永久不再使用时，可手动删除对应的两个会话文件；
- 账号密码仅存在于青龙环境变量，脚本不会写入文件或日志；
- 日志不会输出密码、Cookie、Token。

## 9. 常见问题

**Q：本地运行提示 `NOTIFY: 青龙 notify.py 不可用（本地运行属正常），使用 Webhook`？**
本地没有青龙的 `notify.py`，属正常降级；随后应看到 `NOTIFY_OK: webhook`。到青龙环境后若 `notify.py` 可用则优先使用。

**Q：提示 `FEISHU_FAILED: 未配置 AGENTROUTER_WEBHOOK_URL`？**
脚本运行进程没有读到该变量。注意 `VAR=value` 不会传给子进程，环境变量文件需写 `export VAR=...`，或使用 `set -a; source env文件; set +a`；每个新终端都要重新加载。

**Q：签到前余额一直是“未知”？**
说明运行开始时保存的会话未被站点接受。本站要求 `session` Cookie 与正确的 `New-API-User` 头同时匹配；脚本在登录后会把 user_id 保存到会话附属文件，第二次运行起才能复用会话读取签到前余额。首次运行（或会话过期）为未知属正常，脚本不会用历史余额冒充。

**Q：`checked_in=true` 但余额变化为 0？**
已实测同一天多次登录均返回 `true` 而余额不变，说明 `checked_in` 更可能是“今日已签到状态”，而非“本次登录新发奖励”。脚本只记录原始值，不据此宣称签到成功；真实奖励金额需用跨日首次登录的前后余额差验证。

**Q：提示“登录返回账号与配置不一致”？**
当 `username` 配置为邮箱、而登录响应不含 email 字段时会出现该提示（不影响签到）。可配置 `expected_name` 为站点用户名启用严格对照；或忽略该提示。

**Q：出现 `HUMAN_ACTION_REQUIRED`？**
站点启用了 Turnstile/验证码/MFA。脚本不会绕过，该账号暂停；其他账号继续。此时需评估是否改用浏览器方案。

## 10. 测试

本地 Mock 测试（不联网、不使用真实账号）：

```bash
cd tasks/agentrouter-checkin
python3 -I tests/mock_test.py
```

覆盖：单账号/多账号成功、单账号失败隔离、配置错误、余额查询失败、身份不匹配、通知失败与结果不确定、验证码暂停、有效旧会话、邮箱身份匹配、锁冲突。

## 11. 尚未验证的站点行为

- `checked_in` 的准确语义；
- 登录是否必然触发签到、重复登录是否幂等；
- 每日签到重置时区；
- `quota` 单位与美元换算比例是否长期固定；
- 真实签到奖励金额；
- 会话 Cookie 的实际寿命；
- 风控是否对脚本请求启用 Turnstile/CAPTCHA/MFA。

以上均以真实账号的受控测试结果为准，脚本不会把未验证假设当作结论。
