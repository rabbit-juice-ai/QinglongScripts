# 🍪 Credit Cookie 工具

从本机 Chrome 读取 **credit.linux.do** 的登录 cookie，生成青龙环境变量
`CREDIT_COOKIE_JSON`，用于跳过 linux.do 登录页的 Cloudflare / hCaptcha。

> 这个目录是**本机工具**，不是青龙定时任务。青龙里只需要运行
> `tasks/credit-linux-do/credit_browser.py`。

> ⚠️ **不要直接双击 `index.html`。** 它是一个需要本地 Python 服务配合的页面：
> 浏览器安全策略不允许普通网页读取 HttpOnly cookie，也不允许网页启动 Chrome。
> 请运行 `python3 cookie_tool.py`，它会自动打开 `http://127.0.0.1:8765/`。
> 想少敲命令：macOS 双击 `start.command`，Windows 双击 `start.bat`。

## 为什么需要它

青龙容器里的 Chromium 登录 linux.do 时，可能被 hCaptcha 拦住。已经在 Mac/PC
Chrome 上登录过 credit 的话，可以导出会话 cookie 放到青龙，浏览器会直接复用。

`_forum_session`、`_t`、`auth.session-token` 都是 **HttpOnly cookie**，
网页里的 `document.cookie` 读不到，所以不能用一个普通网页读取。本工具通过
Chrome DevTools Protocol 读取，页面只是本地操作界面。

## 页面工具（推荐）

macOS 直接双击 `start.command`；Windows 双击 `start.bat`。也可以终端运行：

```bash
cd tools/credit-cookie
python3 cookie_tool.py
```

会自动打开 <http://127.0.0.1:8765/>，并尝试启动一个带调试端口的 Chrome。
页面上的按钮：

| 按钮 | 作用 |
|---|---|
| 启动 Chrome | 用独立 profile 启动带 `--remote-debugging-port` 的 Chrome |
| 打开登录页 | 在 Chrome 里打开 <https://credit.linux.do/balance> |
| 读取 Cookie | 读取并生成 Cookie-Editor 兼容 JSON |
| 验证登录 | 打开 `/balance` 并调用 `user-info`，显示昵称和余额 |

步骤：

1. 点「启动 Chrome」；
2. 在弹出的 Chrome 里登录 credit，完成 Cloudflare / 人机验证，直到能看到余额；
3. 回到工具页点「读取 Cookie」→「复制 JSON」；
4. 在青龙「环境变量」新增 `CREDIT_COOKIE_JSON`，粘贴 JSON；
5. 青龙任务继续只跑 `credit_browser.py`。

常用参数：

```bash
python3 cookie_tool.py --port 8766      # 换端口
python3 cookie_tool.py --no-launch      # 不自动启动 Chrome
python3 cookie_tool.py --no-open        # 不自动打开工具页
python3 cookie_tool.py --profile ~/.credit_profile --chrome "/path/to/Chrome"
```

## 命令行导出

```bash
cd tools/credit-cookie

# 只输出 JSON，方便粘贴到青龙环境变量
python3 export_cookies.py --stdout

# 保存为文件
python3 export_cookies.py --out ~/credit_cookies.json

# CDP 没运行时自动启动 Chrome
python3 export_cookies.py --launch --stdout

# 同时导出 cf_clearance（不推荐）
python3 export_cookies.py --include-cf-clearance
```

输出文件权限为 `600`。脚本只在本机读取 cookie，不发送到网络。

## 前提

- Chrome 以调试端口运行：

  ```bash
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.credit_profile" \
    "https://credit.linux.do/balance"
  ```

  页面工具可以自动完成这一步；也可以手动启动。

- 安装 Playwright（只作为 CDP 客户端，不需要 `playwright install`）：

  ```bash
  python3 -m pip install playwright
  ```

- 默认 CDP 地址：`http://127.0.0.1:9222`，可用 `CREDIT_CDP_URL` 或 `--cdp` 修改。

## 常见问题

**Q：连不上 Chrome CDP？**

确认 Chrome 是用 `--remote-debugging-port=9222` 启动的，且没有别的程序占用
9222。可以先用浏览器打开 <http://127.0.0.1:9222/json/version> 检查。

**Q：自动启动 Chrome 后一直等不到 CDP？**

`--user-data-dir` 指定的 profile 可能已经被普通 Chrome 占用。先完全退出那个
Chrome，或换一个 profile：

```bash
python3 cookie_tool.py --profile ~/.credit_profile_tool
```

**Q：提示没有 `linux_do_credit_session_id`？**

说明 Chrome 还没真正登录 credit。先在 Chrome 里打开
<https://credit.linux.do/balance>，完成验证并看到余额后再读取。

**Q：为什么默认不导出 `cf_clearance`？**

`cf_clearance` 和出口 IP、User-Agent 绑定。Mac 上导出的 clearance 到青龙容器
（不同 IP、不同 Chromium）基本一定失效，反而会干扰浏览器重新过 Cloudflare。
让青龙里的浏览器自己完成挑战即可。

**Q：JSON 应该放哪里？**

青龙「环境变量」新增：

```text
CREDIT_COOKIE_JSON=[{"name": ...}]
```

也可以放 `CREDIT_COOKIE`，格式 `name=value; name2=value2; ...`，但 JSON 优先。

## 安全

- 工具页面只监听 `127.0.0.1`，不上传任何数据；
- 生成的 JSON 等同于登录凭据，不要截图、不要发到群里；
- 青龙环境变量对能打开面板的人可见，请限制面板访问权限；
- 不要提交 `credit_cookies.json`（已在 `.gitignore` 中）；
- 会话失效后重新导出一次即可。
