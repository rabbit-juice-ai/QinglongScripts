<div align="center">

# 🐉 QinglongScripts

**青龙面板自动任务脚本仓库，持续收录常用服务的签到/任务脚本。**

**A collection of auto check-in & task scripts for the Qinglong panel.**

[![GitHub stars](https://img.shields.io/github/stars/rabbit-juice-ai/QinglongScripts?style=flat-square&logo=github)](https://github.com/rabbit-juice-ai/QinglongScripts/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/rabbit-juice-ai/QinglongScripts?style=flat-square&logo=github)](https://github.com/rabbit-juice-ai/QinglongScripts/network/members)

</div>

---

## 📥 订阅方式 | How to Subscribe

在青龙面板中添加订阅：

| 配置项 | 值 |
|:---:|:---|
| **名称** | `QinglongScripts` |
| **类型** | 公开仓库 |
| **链接** | `https://github.com/rabbit-juice-ai/QinglongScripts.git` |
| **定时类型** | crontab |
| **定时规则** | `0 0 * * *`（每天自动更新订阅） |
| **白名单** | 留空（拉取全部脚本） |
| **黑名单** | 留空 |

> 💡 本仓库脚本暂不含 `cron` 注释，订阅后不会自动创建定时任务；请在青龙「定时任务」中手动创建，命令见下方脚本说明。

---

## 📜 脚本列表 | Script List

| 脚本 | 功能 | 定时规则 | 说明 |
|:---:|:---|:---:|:---|
| 🎯 `agentrouter_checkin.py` | AgentRouter 多账号签到与余额汇总 | 手动配置（建议每天一次） | 单文件 Python，纯标准库；动态账号；飞书汇总；无数据库、无 Web |

脚本路径：`tasks/agentrouter-checkin/agentrouter_checkin.py`

---

## ⚙️ AgentRouter-Checkin 环境变量 | Task Environment Variables

> 💡 以下变量**仅属于 AgentRouter-Checkin 任务**（`tasks/agentrouter-checkin/`），用于该脚本的登录与通知。**本仓库本身不需要任何环境变量**；后续新增任务会各自声明所需变量。

请在青龙面板的「环境变量」中配置：

| 变量 | 必填 | 说明 |
|:---|:---:|:---|
| `AGENTROUTER_ACCOUNTS` | ✅ | 账号 JSON 数组，支持任意数量（1～20） || `PROXY_URL` | ⬜ | 显式代理，例如 `http://192.168.31.29:37890`；留空时自动使用 `HTTPS_PROXY`/`HTTP_PROXY` |
| `AGENTROUTER_WEBHOOK_URL` | ⬜ | 飞书自定义机器人 Webhook；未配置时仅用青龙 `notify.py` |
| `AGENTROUTER_DATA_DIR` | ⬜ | 运行时数据目录，默认 `/ql/data/agentrouter-checkin` |
| `AGENTROUTER_BASE_URL` | ⬜ | 站点地址，默认 `https://agentrouter.org` |

### 账号配置示例

```json
[
  {
    "username": "你的登录用户名或邮箱",
    "password": "你的密码",
    "enabled": true,
    "expected_name": "网站上的用户名（可选）"
  }
]
```

> 💡 `username` 同时是账号唯一标识；`expected_name` 可选，配置后启用严格身份对照（比对站点返回的 username/email），不配置则“登录成功即成功”。

### 青龙任务命令

```text
cd /ql/data/scripts/<脚本所在目录> && python3 agentrouter_checkin.py --mode full
```

> 💡 建议任务超时 ≥ 35 分钟；如支持“不允许并发”请开启。首次部署先跑 `--mode dry-run`、`--mode site-connectivity`，再跑单账号验证。

---

## 📣 通知推送 | Notifications

所有账号执行结束后统一发送一条汇总，按以下顺序选择渠道：

| 优先级 | 渠道 | 配置方式 | 说明 |
|:---:|:---|:---|:---|
| 1️⃣ | 青龙 `notify.py` | 青龙「通知设置」 | 存在且可用时优先使用 |
| 2️⃣ | 飞书自定义机器人 | `AGENTROUTER_WEBHOOK_URL` | notify.py 不可用或明确失败时回退 |
| — | 控制台/任务日志 | 无需配置 | 汇总始终打印到青龙任务日志 |

> 💡 通知结果不确定时不自动重发；通知失败不影响签到结果。消息不含密码、Cookie、Token。

---

## 📖 使用文档 | Documentation

完整使用说明（配置、模式、退出码、常见问题、已知未验证行为）见：

- [docs/agentrouter-checkin.md](docs/agentrouter-checkin.md)

运行模式速查：

| 模式 | 命令 | 说明 |
|:---:|:---|:---|
| 全量 | `--mode full` | 执行全部启用账号 |
| 单账号 | `--mode single --account <username>` | 只执行指定账号 |
| 配置校验 | `--mode dry-run` | 仅解析配置，不登录 |
| 站点连通 | `--mode site-connectivity` | 仅请求 `/api/status` |
| 飞书连通 | `--mode feishu-connectivity` | 发送测试消息 |
| 会话诊断 | `--mode session-check --account <username>` | 用已保存 Cookie 检查会话，不登录 |

---

## 📁 项目结构 | Project Structure

```text
QinglongScripts/
├── README.md                       # 项目说明
├── docs/
│   └── agentrouter-checkin.md      # AgentRouter 脚本使用文档
└── tasks/
    └── agentrouter-checkin/
        └── agentrouter_checkin.py  # 唯一运行文件（纯标准库）
```

---

## 🔐 安全说明 | Security

- 本仓库不包含任何真实账号、密码、Cookie、Webhook；
- 凭据只应配置在青龙环境变量或本地未提交的 env 文件中；
- 会话文件（`*.cookies`、会话附属 `.json`）视同凭据，已被 `.gitignore` 排除；
- 运行日志不会输出密码、Cookie、Token。

> ⚠️ 请在使用前确认目标平台的服务条款允许此类自动化操作；本项目不提供绕过验证码、MFA 或风控的能力。

---

## 🤝 贡献 | Contributing

欢迎提交 Issue 和 Pull Request。新增任务请放在 `tasks/<任务名>/` 下，并在本 README 的脚本列表与文档中登记。
