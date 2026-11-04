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

> 💡 本仓库脚本暂不含 `cron` 注释，订阅后不会自动创建定时任务；请在青龙「定时任务」中手动创建。
>
> ⚠️ 若仓库为私有，青龙无法匿名拉取（会提示 `could not read Username`）。请先将仓库设为公开，或手动上传脚本，详见各脚本的使用文档。

---

## 📜 脚本列表 | Script List

| 脚本 | 功能 | 定时规则 | 文档 |
|:---:|:---|:---:|:---:|
| 🎯 `agentrouter_checkin.py` | AgentRouter 多账号签到与余额汇总 | 手动配置（建议每天一次） | [使用文档](docs/agentrouter-checkin.md) |

> 💡 脚本路径：`tasks/agentrouter-checkin/agentrouter_checkin.py`；环境变量、任务命令、运行模式等均见对应文档。

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

欢迎提交 Issue 和 Pull Request。新增任务请放在 `tasks/<任务名>/` 下，并在本 README 的脚本列表中登记脚本与文档链接。
