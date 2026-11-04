# QinglongScripts

青龙面板（QingLong）定时任务脚本集合。每个任务独立放在 `tasks/` 下，互不依赖。

## 任务列表

| 任务 | 说明 | 文档 |
|---|---|---|
| AgentRouter-Checkin | AgentRouter 多账号每日签到与余额汇总（单文件 Python，纯标准库） | [docs/agentrouter-checkin.md](docs/agentrouter-checkin.md) |

## AgentRouter-Checkin 快速开始

- 脚本：`tasks/agentrouter-checkin/agentrouter_checkin.py`
- 测试：`tasks/agentrouter-checkin/tests/mock_test.py`
- 依赖：Python 3.9+，无第三方依赖；不需要 Chromium/Playwright
- 数据：无数据库；仅保存每账号会话文件（`AGENTROUTER_DATA_DIR`，默认 `/ql/data/agentrouter-checkin`）

### 1. 青龙环境变量

```text
AGENTROUTER_ACCOUNTS=[{"username":"你的用户名或邮箱","password":"你的密码","enabled":true}]
```

可选：

```text
PROXY_URL=http://192.168.31.29:37890        # 直连不通时配置；也可改用 HTTPS_PROXY/HTTP_PROXY
AGENTROUTER_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
AGENTROUTER_DATA_DIR=/ql/data/agentrouter-checkin
```

账号字段：`username`（必填，唯一标识）、`password`（必填）、`enabled`（必填）、`expected_name`（可选，严格身份对照）。完整说明见[使用文档](docs/agentrouter-checkin.md)。

### 2. 青龙任务命令

```text
cd /ql/data/scripts/<脚本所在目录> && python3 agentrouter_checkin.py --mode full
```

建议任务超时 ≥ 35 分钟；如支持“不允许并发”请开启。首次部署先跑 `--mode dry-run`、`--mode site-connectivity`，再跑单账号验证。

### 3. 本地测试

```bash
cd tasks/agentrouter-checkin
python3 -I tests/mock_test.py
```

全部使用 Mock，不访问真实站点、不使用真实账号。

## 目录结构

```text
QinglongScripts/
├── README.md
├── docs/
│   └── agentrouter-checkin.md      # 使用文档
└── tasks/
    └── agentrouter-checkin/
        ├── agentrouter_checkin.py  # 唯一运行文件
        └── tests/
            └── mock_test.py        # Mock 测试
```

## 安全说明

- 本仓库不包含任何真实账号、密码、Cookie、Webhook；
- 凭据只应配置在青龙环境变量或本地未提交的 env 文件中；
- 会话文件（`*.cookies`、会话附属 `.json`）视同凭据，已被 `.gitignore` 排除；
- 运行日志不会输出密码、Cookie、Token。
