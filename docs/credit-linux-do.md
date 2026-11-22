# LINUX DO Credit 自动登录使用文档

提供两种运行模式：

| 模式 | 脚本 | 适用场景 |
|:---:|:---|:---|
| **浏览器模式（推荐）** | `credit_browser.py` | 青龙容器里已有 Chromium；用账号密码自动完成登录，浏览器持久化 |
| **Cookie 模式（备用）** | `credit_login.py` | 不想在青龙里跑浏览器；从本机导出 cookie，纯 HTTP 复用 |

> 两种模式都不绕过 Cloudflare Turnstile / 人机验证。浏览器模式用真实的有头
> Chromium 让 Cloudflare 的 JS 正常执行；如果 CF 要求人工点击，脚本会停止并提示。

---

## 1. 浏览器模式（推荐）

### 1.1 功能

脚本在青龙容器里完成你要求的完整流程：

1. 打开 <https://credit.linux.do/login>；
2. 勾选「我已阅读并同意 服务条款 及 隐私政策」；
3. 点击「使用 LINUX DO 登录」；
4. 在 linux.do 登录页填写 `LINUXDO_USERNAME` / `LINUXDO_PASSWORD`；
5. 等待 Cloudflare Turnstile 由真实浏览器完成（脚本不点击、不破解）；
6. 点击 linux.do「登录」；
7. 在 LINUX DO Connect 授权页点击「允许」；
8. 等待 credit 回调写入新会话；
9. 输出账号、可用余额、社区积分、待结算；
10. 发送飞书通知（Webhook 优先，未配置时回退青龙 `notify.py`）。

后续运行会复用浏览器 profile，不再重复登录；只有 credit 会话失效时才重新走登录。

### 1.2 环境要求

| 项目 | 要求 |
|---|---|
| Chromium | 青龙容器里已安装（你已安装） |
| Xvfb | 需要安装，给 Chromium 提供虚拟显示器 |
| Python | 3.9+（青龙自带即可） |
| Playwright | Python 包，只作为 CDP 客户端，不需要下载浏览器 |
| 网络 | 能访问 `credit.linux.do`、`connect.linux.do`、`linux.do` |
| 磁盘 | 浏览器 profile 约 200–400 MB |

在青龙容器里安装：

```bash
apt-get update
apt-get install -y xvfb fonts-noto-cjk
pip3 install playwright
```

也可以在青龙「依赖管理」里安装：

| 类型 | 名称 |
|---|---|
| Linux | `xvfb` |
| Linux | `fonts-noto-cjk` |
| Python3 | `playwright` |

> 说明：`playwright install` 不需要执行。脚本用 `executable_path` 指向青龙里
> 已安装的 Chromium，Playwright 只负责通过 CDP 控制它。

检查 Chromium 是否可用：

```bash
which chromium || which chromium-browser || which google-chrome
chromium --version
```

如果找不到，安装：

```bash
apt-get install -y chromium
```

### 1.3 青龙环境变量

在青龙「环境变量」中新增：

| 变量 | 必填 | 说明 |
|---|:---:|---|
| `LINUXDO_USERNAME` | 是 | linux.do 用户名或邮箱 |
| `LINUXDO_PASSWORD` | 是 | linux.do 密码 |
| `CREDIT_PROFILE_DIR` | 否 | 浏览器 profile 目录，默认 `/ql/data/credit-linux-do/browser-profile` |
| `CREDIT_CHROMIUM_BIN` | 否 | Chromium 路径；默认自动查找 `chromium` / `chromium-browser` / `google-chrome` |
| `CREDIT_CDP_PORT` | 否 | CDP 起始端口，默认 `9222`；被占用时自动往后找 |
| `CREDIT_CDP_URL` | 否 | 连接已有的 Chromium，例如 `http://127.0.0.1:9222`；一般不用 |
| `CREDIT_BROWSER_PROXY` | 否 | Chromium 代理，例如 `http://192.168.1.2:7897` |
| `CREDIT_CF_TIMEOUT` | 否 | 等待 Cloudflare/Turnstile 的秒数，默认 `60` |
| `CREDIT_LOGIN_TIMEOUT` | 否 | 等待登录跳转的秒数，默认 `60` |
| `CREDIT_WEBHOOK_URL` | 否 | 飞书自定义机器人 Webhook；未配置时回退青龙 `notify.py` |
| `CREDIT_WEBHOOK_SECRET` | 否 | 飞书机器人「签名校验」密钥；只有开启签名校验才需要 |
| `CREDIT_WEBHOOK_PROXY` | 否 | 飞书通知专用代理（HTTP/HTTPS）；不填则用 `CREDIT_BROWSER_PROXY` 或 `PROXY_URL` |
| `CREDIT_NOTIFY_ON_ERROR` | 否 | 失败时是否发通知，默认 `1`；设 `0` 关闭 |
| `DEBUG` | 否 | 设为 `1` 输出调试信息（不会输出密码） |

> ⚠️ **安全提示**
>
> - 密码放在青龙环境变量里，能打开青龙面板的人都能看到；请限制面板访问权限。
> - 建议给青龙单独使用一个高强度密码，不要和其他站点复用。
> - 如果 linux.do 账号启用了两步验证，脚本会停在验证码页面并以退出码 2 提示，
>   需要人工完成或改用 Cookie 模式。脚本不提供绕过 2FA 的能力。

### 1.4 新建青龙任务

| 配置项 | 值 |
|---|---|
| 命令 | 订阅任务：`task rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do/credit_browser.py`<br>手动任务：`cd /ql/data/repo/rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do && python3 credit_browser.py` |
| 定时 | `0 9 * * *`（每天 9 点，可按需修改） |
| 超时 | 建议 ≥ 5 分钟（首次登录要等 Cloudflare 和页面加载） |
| 并发 | 不允许并发 |

先手动运行一次，首次成功日志大致如下：

```text
[*] 启动 Chromium：/usr/bin/chromium
[+] 已启动 Xvfb（DISPLAY=:99）
[+] Chromium 已就绪（CDP 9222）
[*] credit 会话无效，开始自动登录 …
[+] 已勾选服务条款
[*] 点击「使用 LINUX DO 登录」…
[+] 已填写 linux.do 账号密码
[+] Cloudflare Turnstile 已通过
[+] linux.do 登录成功
[*] 进入 LINUX DO Connect 授权页
[*] 点击「允许」…
[*] 等待 credit 完成 OAuth 回调 …
[+] 登录完成
[+] 当前会话有效
[+] 用户：xxx (@xxx)
[+] 可用余额：xxx LDC
[+] 社区积分：xxx
[+] 待结算：0 LDC
[+] 飞书通知已发送（Webhook）
```

后续运行日志：

```text
[*] 复用上次启动的 Chromium（CDP 9222）
[+] 当前会话有效
[+] 用户：xxx (@xxx)
...
```

浏览器会保持运行，profile 持久化；下次任务直接复用。需要停止浏览器：

```bash
cd /ql/data/repo/rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do
python3 credit_browser.py --close
```

### 1.5 Cloudflare 说明（重要）

- 青龙容器没有显示器。Chromium 以 `--headless` 运行时，实测会被 Cloudflare
  卡在「请稍候… / Just a moment...」页面。
- 脚本用 **Xvfb** 提供一个虚拟显示器，让 Chromium 以普通**有头模式**运行。
  Cloudflare 的 JS 能正常执行；在 Mac 上的实测中，linux.do 登录页的 Turnstile
  token 由浏览器自动填充。
- 脚本**不修改浏览器指纹、不调用打码平台、不破解 Turnstile**。如果 Cloudflare
  要求人工点击，脚本会以退出码 2 停止，并给出提示。
- 这时可以：
  1. 在 Mac 的浏览器里登录 <https://credit.linux.do/balance>；
  2. 用 Cookie-Editor 导出 cookie（至少 `linux_do_credit_session_id`、
     `cf_clearance`、`auth.session-token`）；
  3. 把 JSON 放到青龙，然后执行：
     ```bash
     python3 credit_browser.py --import-cookies /ql/data/credit-linux-do/credit_cookies.json --check
     ```
     浏览器会复用这份登录态；之后仍由浏览器自己刷新 Cloudflare。
  4. 进阶：在青龙容器里安装 noVNC，用网页 VNC 手动点一次验证。这个方案依赖较多，
     不在本文默认步骤里。

> 不要相信「无头浏览器自动过 CF」的说法。本脚本不做绕过验证的事。

### 1.6 飞书通知

成功和失败都会发送飞书通知（失败通知可用 `CREDIT_NOTIFY_ON_ERROR=0` 关闭）。

推荐配置飞书自定义机器人 Webhook：

1. 在飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人；
2. 复制 Webhook 地址；
3. 青龙环境变量新增：
   ```text
   CREDIT_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
   ```
4. 如果机器人开启了「签名校验」，再设置：
   ```text
   CREDIT_WEBHOOK_SECRET=机器人页面显示的密钥
   ```

成功通知内容：

```text
LINUX DO Credit 余额
用户：xxx (@xxx)
可用余额：xxx.xx LDC
社区积分：882
待结算：0 LDC
时间：2026-11-22 09:05:36
```

失败通知会包含退出原因，例如：

```text
LINUX DO Credit 任务失败
linux.do 登录失败：账号或密码不正确，或触发了风控。
时间：2026-11-22 09:05:36
```

通知发送顺序：

1. 配置了 `CREDIT_WEBHOOK_URL`：优先直接发飞书 Webhook；
2. 未配置或发送失败：回退青龙 `notify.py`，使用青龙「通知设置」里的渠道；
3. 两者都不可用：只在任务日志输出，不影响任务退出码。

> Webhook URL 本身是凭据，不要提交 Git、不要发到群里。NAS 访问飞书需要代理时，
> 可设置 `CREDIT_WEBHOOK_PROXY`（只支持 HTTP/HTTPS 代理）。

### 1.7 其他命令

| 命令 | 作用 |
|---|---|
| `python3 credit_browser.py` | 检查会话；失效则自动登录 |
| `python3 credit_browser.py --check` | 只检查会话，不自动登录 |
| `python3 credit_browser.py --login` | 强制清除 credit 会话并重新登录 |
| `python3 credit_browser.py --import-cookies FILE ...` | 把 Cookie-Editor 导出的 cookie 导入浏览器 profile |
| `python3 credit_browser.py --close` | 停止由脚本启动的 Chromium/Xvfb |
| `python3 credit_browser.py --debug` | 输出调试信息 |

示例：

```bash
cd /ql/data/repo/rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do

# 只检查
python3 credit_browser.py --check

# 强制重新登录
python3 credit_browser.py --login

# 导入 Mac 上导出的 cookie 并检查
python3 credit_browser.py --import-cookies /ql/data/credit-linux-do/credit_cookies.json --check
```

### 1.8 退出码

| 退出码 | 含义 | 处理 |
|:---:|---|---|
| 0 | 会话有效，或登录成功 | 无需处理 |
| 1 | 网络、页面加载等一般错误 | 看日志，稍后重试 |
| 2 | 需要人工处理：密码错误、2FA、邮箱验证、Turnstile 人工点击、页面改版 | 按日志提示处理 |
| 3 | Cloudflare 挑战没有通过 | 重试；或从 Mac 导入 cookie |
| 4 | 缺少 Chromium / Xvfb / Playwright | 按 1.2 安装 |

### 1.9 常见问题

**Q：日志提示「未找到 Xvfb」？**

在青龙容器里执行：

```bash
apt-get update && apt-get install -y xvfb fonts-noto-cjk
```

或在青龙「依赖管理 → Linux」中添加 `xvfb`。

**Q：日志提示「未安装 Playwright」？**

在青龙「依赖管理 → Python3」中添加 `playwright`，或执行：

```bash
pip3 install playwright
```

不需要执行 `playwright install`。

**Q：日志提示「Cloudflare Turnstile 需要人工完成」？**

真实浏览器没有自动通过验证。脚本不会绕过，处理方式见 1.5：从 Mac 导入
cookie，或安装 noVNC 手动点一次。

**Q：日志提示「linux.do 登录失败：账号或密码不正确」？**

检查 `LINUXDO_USERNAME` / `LINUXDO_PASSWORD`。不要连续重试，避免触发风控。
如果账号开了 2FA，脚本会单独提示。

**Q：日志提示「该 linux.do 账号启用了两步验证」？**

脚本不能代替你输入 2FA 验证码。可以：

- 在青龙里临时关闭 2FA，登录成功后浏览器会保持会话；或
- 改用 Cookie 模式，从已登录的浏览器导出 cookie。

**Q：日志提示「linux.do 要求验证邮箱（新设备登录）」？**

去邮箱完成验证，然后重新运行。浏览器 profile 会保留这次登录，后续一般不会再要求。

**Q：会不会每天重新登录？**

不会。浏览器 profile 持久化，credit 会话约 7 天有效；只有会话失效时才重新走
登录流程。Cloudflare 由浏览器自己维护。

**Q：浏览器一直运行会不会占很多内存？**

Chromium 常驻大约 200–500 MB。NAS 内存紧张时，可以在任务结束后执行
`python3 credit_browser.py --close`，但下次运行需要重新启动浏览器（会话仍在
profile 里，只是启动慢一点）。

**Q：端口 9222 被占用？**

脚本会自动尝试 9222–9241。也可以设置 `CREDIT_CDP_PORT` 指定起始端口。
如果日志显示连接到了不认识的浏览器，请设置 `CREDIT_CDP_URL` 指向正确的
CDP 地址，或先停掉占用端口的程序。

**Q：怎么确认浏览器 profile 在哪个目录？**

默认 `/ql/data/credit-linux-do/browser-profile`。这个目录等同于登录态，权限
应为 `700`，不要提交 Git、不要分享。

**Q：日志提示 `BrowserType.connect_over_cdp: Timeout 30000ms exceeded`？**

说明复用到了一个无响应的旧 Chromium。脚本会自动清理并重新启动；也可以手动处理：

```bash
cd /ql/data/repo/rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do
python3 credit_browser.py --close
python3 credit_browser.py
```

如果还不行，检查 9222 是否被别的程序占用：

```bash
ss -lntp | grep 9222
ps -ef | grep -i '[c]hrom'
```

**Q：怎么没收到飞书通知？**

按日志检查：

- `[+] 飞书通知已发送（Webhook）`：已经发出；
- `[*] 未配置 CREDIT_WEBHOOK_URL，尝试青龙 notify.py`：没有配 Webhook，检查青龙「通知设置」；
- `[!] 未发送飞书通知`：两个渠道都不可用，配置 `CREDIT_WEBHOOK_URL` 即可。

飞书机器人如果开了「签名校验」，必须同时设置 `CREDIT_WEBHOOK_SECRET`；如果开了
「自定义关键词」，消息里需要包含关键词（默认标题包含 `LINUX DO Credit`）。

**Q：通知会包含密码或 cookie 吗？**

不会。成功通知只包含用户昵称、余额、积分、待结算和时间；失败通知会去掉 URL
的查询串，避免把 OAuth code/state 发出去。

---

## 2. Cookie 模式（备用）

如果不想在青龙里跑 Chromium，可以用 `credit_login.py`：它只用 Python 标准库，
复用浏览器导出的 3 个 cookie，并在 credit 会话过期时用 Connect 会话自动走
OAuth 续期。

> Cookie 模式目前只把结果输出到任务日志，不发飞书；需要飞书余额通知请用上面的
> 浏览器模式。

### 2.1 原理

| Cookie | Domain | 作用 | 有效期 |
|---|---|---|---|
| `linux_do_credit_session_id` | `.linux.do` | credit 站自己的会话 | 约 7 天 |
| `cf_clearance` | `.linux.do` | Cloudflare 通行证 | 与出口 IP、UA 绑定 |
| `auth.session-token` | `connect.linux.do` | Connect 会话，用于 OAuth 续期 | 服务端会话 |

脚本每次运行先调 `/api/v1/oauth/user-info`；401 时用 `auth.session-token` 走
`oauth/login → authorize → approve → /api/v1/oauth/callback` 换新会话。

### 2.2 一次性导出 cookie

1. 用 Chrome 打开 <https://credit.linux.do/balance>；
2. 完成 Cloudflare 验证并登录；
3. 打开开发者工具 → Application → Cookies；
4. 复制三个 cookie 的值：
   - `linux_do_credit_session_id`（`credit.linux.do` 或 `.linux.do`）
   - `cf_clearance`（`.linux.do`）
   - `auth.session-token`（`connect.linux.do`）
5. 保存为 `credit_cookies.json`：

```json
[
  {
    "name": "linux_do_credit_session_id",
    "value": "粘贴实际值",
    "domain": ".linux.do",
    "path": "/",
    "expires": null
  },
  {
    "name": "cf_clearance",
    "value": "粘贴实际值",
    "domain": ".linux.do",
    "path": "/",
    "expires": null
  },
  {
    "name": "auth.session-token",
    "value": "粘贴实际值",
    "domain": "connect.linux.do",
    "path": "/",
    "expires": null
  }
]
```

6. 设置权限：

```bash
chmod 600 credit_cookies.json
```

### 2.3 青龙配置

把 `credit_cookies.json` 放到：

```text
/ql/data/credit-linux-do/credit_cookies.json
```

新建任务：

| 配置项 | 值 |
|---|---|
| 命令 | 订阅任务：`task rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do/credit_login.py`<br>手动任务：`cd /ql/data/repo/rabbit-juice-ai_QinglongScripts/tasks/credit-linux-do && python3 credit_login.py` |
| 定时 | `0 9 * * *` |
| 超时 | ≥ 2 分钟 |
| 并发 | 不允许并发 |

环境变量（可选）：

| 变量 | 说明 |
|---|---|
| `CREDIT_COOKIE` | `name=value; ...` 形式的 cookie |
| `CREDIT_COOKIE_JSON` | JSON 数组/对象形式的 cookie |
| `CREDIT_COOKIE_FILE` | 状态文件路径 |
| `CREDIT_FORCE_ENV` | 设为 `1` 时忽略状态文件 |
| `CREDIT_UA` | 覆盖 User-Agent |
| `DEBUG` | 设为 `1` 输出 OAuth 调试信息 |

### 2.4 退出码

| 退出码 | 含义 | 处理 |
|:---:|---|---|
| 0 | 会话有效，或自动续期成功 | 无需处理 |
| 1 | 网络/解析等一般错误 | 看日志，稍后重试 |
| 2 | 需要人工重新导出 cookie | 按 2.2 重新导出 |
| 3 | Cloudflare 403（cf_clearance 失效或 IP 不一致） | 重新导出；检查出口 IP |

### 2.5 常见问题

**Q：Cloudflare 403，退出码 3？**

`cf_clearance` 失效，或青龙出口 IP 与导出 cookie 的电脑不一致。先在青龙里查
公网 IP：

```bash
curl -s https://api.ipify.org; echo
```

与导出 cookie 的电脑对比。如果不一致，让 NAS 走与电脑相同的代理，或在相同
出口的网络里重新导出。

**Q：HTTP 429 / RATE_LIMITED？**

短时间重复登录触发限流。等 1–2 分钟再跑；每天一次即可。

**Q：退出码 2，提示 Connect 会话已失效？**

`auth.session-token` 过期了。重新导出一次即可。

**Q：只导出 credit 的两个 cookie 行不行？**

可以查余额；但 credit 会话过期后无法自动续期，会以退出码 2 提示重新导出。

---

## 3. 数据与安全

- 浏览器 profile、`credit_cookies.json` 都等同于账号登录态；
- 飞书 Webhook URL、签名密钥同样是凭据，不要提交 Git 或分享；
- profile 目录权限建议 `700`，cookie 文件权限 `600`；
- 不要提交 Git、不要分享、不要发到群里；
- 脚本日志不会输出密码、cookie 值；`DEBUG=1` 也只输出页面地址和 cookie 名称/长度；
- 如果怀疑泄露：在 linux.do 退出所有设备，修改密码，并删除青龙里的 profile 和
  cookie 文件后重新登录；
- 请确认目标平台的服务条款允许此类自动化操作。本项目不提供绕过验证码、MFA
  或风控的能力。
