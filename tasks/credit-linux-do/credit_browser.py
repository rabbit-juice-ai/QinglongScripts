#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LINUX DO Credit 浏览器自动登录（青龙 / Chromium + Xvfb + Playwright）。

站点：https://credit.linux.do

流程
----
1. 启动（或复用）青龙容器里的 Chromium，profile 持久化；
2. 打开 https://credit.linux.do/balance，检查 credit 会话；
3. 会话无效时打开 /login：
   - 勾选服务条款；
   - 点击「使用 LINUX DO 登录」；
   - 在 linux.do 登录页填写 LINUXDO_USERNAME / LINUXDO_PASSWORD；
   - 等待 Cloudflare Turnstile 由真实浏览器完成（脚本不绕过验证）；
   - 完成 LINUX DO Connect 授权；
   - 等待 credit 回调并写入新会话；
4. 输出账号与余额，浏览器保持运行，供下次复用。

为什么需要 Xvfb
---------------
青龙容器没有显示器。Chromium 以 headless 模式运行会被 Cloudflare 拦截；
用 Xvfb 提供一个虚拟显示器，让 Chromium 以普通有头模式运行，Cloudflare
的 JS 才能正常执行。脚本不修改浏览器指纹、不破解验证码；如果 Turnstile
要求人工点击，脚本会停止并提示。

依赖
----
* Chromium（青龙容器里已安装，或 apt-get install chromium）
* Xvfb（apt-get install xvfb；青龙「依赖管理 → Linux」也可安装）
* Python 包 playwright（青龙「依赖管理 → Python3」或 pip3 install playwright）
  只使用 Playwright 的 CDP 客户端，不需要 playwright install 下载浏览器。

用法
----
    python3 credit_browser.py                 # 检查会话，失效则自动登录
    python3 credit_browser.py --check         # 只检查，不自动登录
    python3 credit_browser.py --login         # 强制清除 credit 会话并重新登录
    python3 credit_browser.py --import-cookies FILE [FILE ...]
    python3 credit_browser.py --close         # 停止由脚本启动的 Chromium/Xvfb

运行结果会发飞书通知：优先 CREDIT_WEBHOOK_URL，未配置/失败时回退青龙
notify.py；失败通知可用 CREDIT_NOTIFY_ON_ERROR=0 关闭。

环境变量
--------
    LINUXDO_USERNAME        linux.do 用户名或邮箱（登录时必填）
    LINUXDO_PASSWORD        linux.do 密码（登录时必填）
    CREDIT_PROFILE_DIR      浏览器 profile 目录
    CREDIT_CHROMIUM_BIN     Chromium 可执行文件路径
    CREDIT_CDP_PORT         CDP 端口，默认 9222
    CREDIT_CDP_URL          连接已有浏览器，例如 http://127.0.0.1:9222
    CREDIT_BROWSER_PROXY    Chromium 代理，例如 http://192.168.1.2:7897
    CREDIT_CF_TIMEOUT       Cloudflare/Turnstile 等待秒数，默认 60
    CREDIT_LOGIN_TIMEOUT    登录跳转等待秒数，默认 60
    CREDIT_WEBHOOK_URL      飞书自定义机器人 Webhook（推荐）
    CREDIT_WEBHOOK_SECRET   飞书机器人签名校验密钥（可选）
    CREDIT_WEBHOOK_PROXY    飞书通知专用代理（可选，HTTP/HTTPS）
    CREDIT_NOTIFY_ON_ERROR  失败时是否发通知，默认 1；设 0 关闭
    DEBUG                   设为 1 输出调试信息（不会输出密码）
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib import parse

BASE = "https://credit.linux.do"
LOGIN_URL = f"{BASE}/login"
BALANCE_URL = f"{BASE}/balance"
CONNECT = "https://connect.linux.do"
LINUXDO = "https://linux.do"

DEFAULT_CDP_PORT = 9222
WINDOW_SIZE = "1280,900"
XVFB_SCREEN = "1280x900x24"
CDP_START_TIMEOUT = 30
CF_TIMEOUT = int(os.getenv("CREDIT_CF_TIMEOUT", "60"))
LOGIN_TIMEOUT = int(os.getenv("CREDIT_LOGIN_TIMEOUT", "60"))

CF_TITLES = ("请稍候…", "请稍候...", "Just a moment...", "Just a moment…")

COOKIE_NAMES = {
    "linux_do_credit_session_id",
    "cf_clearance",
    "auth.session-token",
    "_forum_session",
    "_t",
    "sso_payload",
    "_cfuvid",
    "__cf_ob",
}


# ---------------------------------------------------------------------------
# 异常与日志
# ---------------------------------------------------------------------------

class CreditError(Exception):
    """一般错误。"""


class NeedManual(CreditError):
    """需要人工处理（验证码、2FA、页面改版、凭据缺失等）。"""


class CloudflareBlocked(CreditError):
    """Cloudflare 挑战没有通过。"""


class MissingDependency(CreditError):
    """缺少 Chromium / Xvfb / Playwright。"""


class LoginFailed(CreditError):
    """linux.do 明确返回登录失败。"""


def log(message: str) -> None:
    print(message, flush=True)


def debug(message: str) -> None:
    if os.getenv("DEBUG"):
        print(f"[debug] {message}", flush=True)


# ---------------------------------------------------------------------------
# 路径与锁
# ---------------------------------------------------------------------------

def runtime_dir() -> Path:
    ql = Path("/ql/data")
    if ql.is_dir() and os.access(ql, os.W_OK):
        return ql / "credit-linux-do"
    return Path(__file__).resolve().parent / ".runtime"


def default_profile_dir() -> Path:
    return Path(os.getenv("CREDIT_PROFILE_DIR") or (runtime_dir() / "browser-profile"))


def log_path() -> Path:
    return runtime_dir() / "logs" / "chromium.log"


def acquire_lock():
    path = runtime_dir() / "credit_browser.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise CreditError("另一个 credit_browser.py 实例正在运行，本次退出。")
    return handle


def rotate_log(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass


def tail_file(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_bytes()
        return data[-limit:].decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Chromium / Xvfb
# ---------------------------------------------------------------------------

CHROMIUM_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)

CHROMIUM_PATHS = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/lib/chromium/chromium",
    "/opt/google/chrome/chrome",
)


def find_chromium() -> str:
    explicit = os.getenv("CREDIT_CHROMIUM_BIN")
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise MissingDependency(f"CREDIT_CHROMIUM_BIN 指向的文件不存在：{explicit}")

    for name in CHROMIUM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in CHROMIUM_PATHS:
        if Path(candidate).is_file():
            return candidate
    raise MissingDependency(
        "未找到 Chromium。青龙容器可执行：\n"
        "  apt-get update && apt-get install -y chromium\n"
        "或在青龙「依赖管理 → Linux」中添加 chromium。"
    )


def ensure_display() -> Optional[subprocess.Popen]:
    """没有 DISPLAY 时启动 Xvfb，返回 Xvfb 进程（已有 DISPLAY 则返回 None）。"""
    if os.getenv("DISPLAY"):
        debug(f"使用已有 DISPLAY={os.environ['DISPLAY']}")
        return None

    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise MissingDependency(
            "未找到 Xvfb。青龙容器需要先安装：\n"
            "  apt-get update && apt-get install -y xvfb fonts-noto-cjk\n"
            "或在青龙「依赖管理 → Linux」中添加 xvfb。"
        )

    log_path().parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path(), "ab")
    for number in range(99, 110):
        display = f":{number}"
        lock_file = Path(f"/tmp/.X{number}-lock")
        socket_file = Path(f"/tmp/.X11-unix/X{number}")
        if socket_file.exists():
            os.environ["DISPLAY"] = display
            log(f"[*] 复用已有 Xvfb（DISPLAY={display}）")
            return None
        if lock_file.exists():
            continue
        proc = subprocess.Popen(
            [xvfb, display, "-screen", "0", XVFB_SCREEN, "-nolisten", "tcp"],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(50):
            if socket_file.exists():
                os.environ["DISPLAY"] = display
                (runtime_dir() / "xvfb.pid").write_text(str(proc.pid))
                log(f"[+] 已启动 Xvfb（DISPLAY={display}）")
                return proc
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.terminate()
    raise MissingDependency("无法启动 Xvfb（:99–:109 都被占用）。")


def cdp_version(port: int) -> Optional[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_chromium(profile_dir: Path) -> str:
    """返回 CDP URL。浏览器已在运行则直接复用。"""
    external = os.getenv("CREDIT_CDP_URL")
    if external:
        return external.rstrip("/")

    state_path = runtime_dir() / "cdp_url"
    pid_path = runtime_dir() / "chromium.pid"
    if state_path.is_file():
        saved = state_path.read_text().strip()
        saved_port = parse.urlparse(saved).port
        saved_ok = False
        if saved_port and cdp_version(saved_port):
            try:
                pid = int(pid_path.read_text().strip())
            except (OSError, ValueError):
                pid = 0
            if pid:
                cmdline = pid_cmdline(pid)
                saved_ok = (
                    f"--remote-debugging-port={saved_port}" in cmdline
                    and str(profile_dir) in cmdline
                )
        if saved_ok:
            log(f"[*] 复用上次启动的 Chromium（CDP {saved_port}）")
            return saved
        log("[!] 上次的 Chromium 状态无效，重新启动 …")
        for path in (state_path, pid_path):
            try:
                path.unlink()
            except OSError:
                pass

    base_port = int(os.getenv("CREDIT_CDP_PORT", str(DEFAULT_CDP_PORT)))
    port = None
    for candidate in range(base_port, base_port + 20):
        if port_free(candidate):
            port = candidate
            break
    if port is None:
        raise MissingDependency(
            f"找不到空闲的 CDP 端口（{base_port}–{base_port + 19}）。"
        )

    chromium = find_chromium()
    ensure_display()

    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log_path())

    args = [
        chromium,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        f"--window-size={WINDOW_SIZE}",
    ]
    proxy = os.getenv("CREDIT_BROWSER_PROXY")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append(LOGIN_URL)

    log(f"[*] 启动 Chromium：{chromium}")
    debug("启动参数：" + " ".join(args[:-1]))
    with open(log_path(), "ab") as log_handle:
        proc = subprocess.Popen(
            args,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.time() + CDP_START_TIMEOUT
    while time.time() < deadline:
        if cdp_version(port):
            (runtime_dir() / "chromium.pid").write_text(str(proc.pid))
            state_path.write_text(f"http://127.0.0.1:{port}")
            log(f"[+] Chromium 已就绪（CDP {port}）")
            return f"http://127.0.0.1:{port}"
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    tail = tail_file(log_path())
    if proc.poll() is None:
        proc.terminate()
    raise MissingDependency(
        "Chromium 启动失败或 CDP 端口未就绪。\n"
        f"日志：{log_path()}\n{tail}"
    )


def pid_cmdline(pid: int) -> str:
    """返回进程命令行；Linux 读 /proc，macOS 回退到 ps。"""
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.is_file():
        try:
            return proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return ""


def _kill_pid_file(path: Path, expect: str) -> None:
    if not path.is_file():
        return
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid:
        cmdline = pid_cmdline(pid)
        if expect in cmdline:
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"[+] 已停止进程 {pid}")
                for _ in range(30):
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, signal.SIGKILL)
                    log(f"[+] 已强制停止进程 {pid}")
            except OSError:
                pass
    try:
        path.unlink()
    except OSError:
        pass


def close_browser(profile_dir: Optional[Path] = None) -> None:
    _kill_pid_file(
        runtime_dir() / "chromium.pid",
        str(profile_dir or default_profile_dir()),
    )
    _kill_pid_file(runtime_dir() / "xvfb.pid", "Xvfb")
    try:
        (runtime_dir() / "cdp_url").unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Playwright 会话
# ---------------------------------------------------------------------------

def load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MissingDependency(
            "未安装 Playwright。请在青龙「依赖管理 → Python3」中添加 playwright，"
            "或执行：pip3 install playwright"
        ) from exc
    return sync_playwright


class BrowserSession:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        sync_playwright = load_playwright()
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(
                self.cdp_url, timeout=20000
            )
            if self.browser.contexts:
                self.ctx = self.browser.contexts[0]
            else:
                self.ctx = self.browser.new_context()
            self.page = self._pick_page()
            return self
        except Exception:
            if self.pw:
                try:
                    self.pw.stop()
                except Exception:
                    pass
                self.pw = None
            raise

    def _pick_page(self):
        for page in self.ctx.pages:
            if "credit.linux.do" in page.url or "linux.do" in page.url:
                return page
        if self.ctx.pages:
            return self.ctx.pages[0]
        return self.ctx.new_page()

    def __exit__(self, exc_type, exc, tb):
        # 只断开 Playwright，不关闭 Chromium；下次运行继续复用 profile。
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# 页面操作
# ---------------------------------------------------------------------------

def page_title(page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def url_host(url: str) -> str:
    try:
        return parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def on_credit(page) -> bool:
    host = url_host(page.url)
    return host == "credit.linux.do" or host.endswith(".credit.linux.do")


def wait_cloudflare(page, timeout: int = CF_TIMEOUT) -> bool:
    """等待 Cloudflare 挑战页跳走；返回是否通过。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = page_title(page)
        if title and title not in CF_TITLES:
            return True
        page.wait_for_timeout(1000)
    return False


def goto(page, url: str) -> None:
    debug(f"打开 {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        if "Timeout" not in type(exc).__name__:
            raise
        debug(f"页面加载超时：{url}")
    if not wait_cloudflare(page):
        raise CloudflareBlocked(
            f"Cloudflare 挑战没有通过（{page.url}）。"
            "脚本不会绕过验证，请在浏览器里手动完成，或改用 --import-cookies。"
        )


def wait_for_url_any(page, needles, timeout: int) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        for needle in needles:
            if needle in url:
                return needle
        page.wait_for_timeout(500)
    return None


def api_user(page) -> Optional[dict]:
    """在页面上下文里调用 user-info；返回用户数据，401 返回 None。"""
    if not on_credit(page):
        raise CreditError(f"当前页面不在 credit.linux.do：{page.url}")

    result = page.evaluate(
        """async () => {
          const r = await fetch('/api/v1/oauth/user-info', {
            headers: {
              'Accept': 'application/json, text/plain, */*',
              'X-Requested-With': 'XMLHttpRequest'
            },
            credentials: 'include',
            referrer: location.href
          });
          return {status: r.status, text: await r.text()};
        }"""
    )
    status = int(result.get("status", 0))
    text = result.get("text", "")
    debug(f"user-info HTTP {status}")

    if status == 200:
        payload = json.loads(text)
        return payload.get("data")
    if status == 401:
        return None
    if status == 403 and ("Just a moment" in text or "challenge" in text):
        raise CloudflareBlocked("Cloudflare 拦截了 user-info 请求。")
    raise CreditError(f"user-info 返回 HTTP {status}：{text[:200]}")


def check_session(page) -> Optional[dict]:
    goto(page, BALANCE_URL)
    return api_user(page)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def wait_for_login_or_authorize(page, timeout: int) -> Optional[str]:
    """等待页面进入 linux.do 登录表单、Connect 授权页或 credit 回调页。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        if "connect.linux.do/oauth2/authorize" in url:
            return "authorize"
        if "credit.linux.do/login?code=" in url or "credit.linux.do/balance" in url:
            return "credit"
        try:
            form_ready = bool(
                page.query_selector("#login-account-name, #signin_username")
            )
        except Exception:
            # 页面正在导航，下一轮再判断。
            form_ready = False
        if "linux.do/login" in url and form_ready:
            return "login"
        page.wait_for_timeout(500)
    return None


def wait_turnstile(page, timeout: int = CF_TIMEOUT) -> bool:
    """等待 Cloudflare Turnstile 的隐藏 token 出现；不点击验证码。"""
    try:
        has_widget = bool(
            page.query_selector("input[name='cf-turnstile-response']")
            or page.query_selector("iframe[src*='challenges.cloudflare.com']")
        )
    except Exception:
        has_widget = True
    if not has_widget:
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            element = page.query_selector("input[name='cf-turnstile-response']")
        except Exception:
            element = None
        if element:
            try:
                value = element.evaluate("e => e.value || ''")
            except Exception:
                value = ""
            if len(value) > 20:
                return True
        page.wait_for_timeout(1000)
    return False


def has_login_error(page) -> bool:
    try:
        text = page.inner_text("body")[:4000]
    except Exception:
        return False
    needles = (
        "无效的用户名",
        "无效的电子邮件",
        "密码不正确",
        "Invalid username",
        "Invalid password",
        "尝试登录的次数过多",
        "请稍后再试",
        "Too many attempts",
        "登录失败",
    )
    return any(needle in text for needle in needles)


def has_2fa(page) -> bool:
    selectors = (
        "#login-second-factor",
        "input[name='second_factor_code']",
        "input[autocomplete='one-time-code']",
    )
    try:
        if any(page.query_selector(selector) for selector in selectors):
            return True
    except Exception:
        return False
    try:
        text = page.inner_text("body")[:2000]
    except Exception:
        return False
    return "两步验证" in text or "二次验证" in text


def has_email_verification(page) -> bool:
    try:
        text = page.inner_text("body")[:3000]
    except Exception:
        return False
    needles = (
        "检查您的邮箱",
        "验证您的电子邮件",
        "确认您的电子邮件",
        "check your email",
        "verify your email",
    )
    return any(needle in text.lower() for needle in needles)


def do_linuxdo_login(page, username: str, password: str) -> None:
    if not username or not password:
        raise NeedManual(
            "linux.do 登录表单需要账号密码，但未配置 "
            "LINUXDO_USERNAME / LINUXDO_PASSWORD。"
        )

    log("[*] 已跳转 linux.do 登录页")

    try:
        page.wait_for_selector(
            "#login-account-name, #signin_username",
            timeout=LOGIN_TIMEOUT * 1000,
        )
    except Exception as exc:
        name = type(exc).__name__
        message = str(exc)
        if "Timeout" not in name and "Execution context" not in message:
            raise
        if "connect.linux.do/oauth2/authorize" in page.url:
            # 页面在等待期间已经跳到授权页，交给 login() 处理。
            return
        if "linux.do/login" not in page.url:
            # 页面已经跳走，交给 login() 的后续逻辑处理。
            return
        if not wait_cloudflare(page):
            raise CloudflareBlocked("linux.do 登录页被 Cloudflare 拦截。")
        raise NeedManual("linux.do 登录表单没有出现，页面可能改版或触发了风控。")

    if "linux.do/login" not in page.url:
        return

    if page.query_selector("#login-account-name"):
        page.fill("#login-account-name", username)
        page.fill("#login-account-password", password)
    else:
        page.fill("#signin_username", username)
        page.fill("#signin_password", password)
    log("[+] 已填写 linux.do 账号密码")

    if not wait_turnstile(page):
        raise NeedManual(
            "Cloudflare Turnstile 需要人工完成（页面没有自动通过）。"
            "请在浏览器里点一下验证，或从 Mac 导出 cookie 后用 --import-cookies 导入。"
        )
    log("[+] Cloudflare Turnstile 已通过")

    if page.query_selector("#login-button"):
        try:
            page.wait_for_selector("#login-button:not([disabled])", timeout=10000)
        except Exception:
            pass
        page.click("#login-button")
    else:
        page.click("#signin-button")

    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if "linux.do/login" not in page.url:
            log("[+] linux.do 登录成功")
            return
        if has_2fa(page):
            raise NeedManual("该 linux.do 账号启用了两步验证，需要人工完成。")
        if has_email_verification(page):
            raise NeedManual("linux.do 要求验证邮箱（新设备登录），请先在邮箱里完成验证。")
        if has_login_error(page):
            raise LoginFailed("linux.do 登录失败：账号或密码不正确，或触发了风控。")
        page.wait_for_timeout(500)

    raise NeedManual("linux.do 登录后没有跳转，可能触发了风控或需要人工验证。")


def handle_authorize(page) -> None:
    log("[*] 进入 LINUX DO Connect 授权页")
    if not wait_cloudflare(page):
        raise CloudflareBlocked("授权页被 Cloudflare 拦截。")
    if on_credit(page):
        return

    # 优先直接打开 approve 链接，避免弹窗/按钮定位问题。
    link = page.query_selector("a[href*='/oauth2/approve/']")
    if link:
        href = link.get_attribute("href") or ""
        target = parse.urljoin(page.url, href)
        log("[*] 点击「允许」…")
        goto(page, target)
        wait_for_url_any(
            page,
            ("credit.linux.do/login?code=", "credit.linux.do/balance"),
            LOGIN_TIMEOUT,
        )
        return

    selectors = (
        "button:has-text('允许')",
        "button:has-text('授权')",
        "button:has-text('Authorize')",
        "button:has-text('Allow')",
        "a:has-text('允许')",
        "a:has-text('授权')",
        "a:has-text('Authorize')",
        "a:has-text('Allow')",
        "input[type='submit'][value*='允许']",
        "input[type='submit'][value*='授权']",
    )
    for selector in selectors:
        element = page.query_selector(selector)
        if element:
            log(f"[*] 点击授权按钮（{selector}）…")
            element.click()
            wait_for_url_any(
                page,
                ("credit.linux.do/login?code=", "credit.linux.do/balance"),
                LOGIN_TIMEOUT,
            )
            return

    # 有些授权页需要先勾选同意，再点提交。
    checkbox = page.query_selector("input[type='checkbox']")
    if checkbox:
        if not checkbox.is_checked():
            checkbox.check()
            log("[+] 已勾选授权同意")
        for selector in (
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('继续')",
            "button:has-text('允许')",
        ):
            element = page.query_selector(selector)
            if element:
                element.click()
                wait_for_url_any(
                    page,
                    ("credit.linux.do/login?code=", "credit.linux.do/balance"),
                    LOGIN_TIMEOUT,
                )
                return

    match = re.search(r"/oauth2/approve/[A-Za-z0-9_-]+", page.content())
    if match:
        target = parse.urljoin(CONNECT, match.group(0))
        log("[*] 直接打开授权链接 …")
        goto(page, target)
        wait_for_url_any(
            page,
            ("credit.linux.do/login?code=", "credit.linux.do/balance"),
            LOGIN_TIMEOUT,
        )
        return

    raise NeedManual(f"授权页没有找到「允许」按钮，当前地址：{page.url}")


def finish_oauth(page) -> dict:
    needle = wait_for_url_any(
        page,
        ("credit.linux.do/login?code=", "credit.linux.do/balance"),
        LOGIN_TIMEOUT,
    )
    if not needle:
        raise NeedManual(f"OAuth 授权后没有回到 credit，当前地址：{page.url}")

    if "credit.linux.do/login?code=" in page.url:
        log("[*] 等待 credit 完成 OAuth 回调 …")
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline and "/balance" not in page.url:
            try:
                user = api_user(page)
                if user:
                    return user
            except CloudflareBlocked:
                raise
            except CreditError:
                pass
            page.wait_for_timeout(1000)

    goto(page, BALANCE_URL)
    for attempt in range(3):
        user = api_user(page)
        if user:
            return user
        if attempt < 2:
            page.wait_for_timeout(2000)
    raise NeedManual("OAuth 流程完成，但 credit 会话仍然无效。")


def login(page, username: str, password: str) -> None:
    log("[*] credit 会话无效，开始自动登录 …")
    goto(page, LOGIN_URL)
    if "/balance" in page.url:
        log("[+] 已经登录")
        return

    # 登录页是前端渲染的，domcontentloaded 之后按钮可能还没出现。
    try:
        page.wait_for_selector(
            "#terms, button:has-text('使用 LINUX DO 登录')",
            timeout=30000,
        )
    except Exception as exc:
        if "Timeout" not in type(exc).__name__:
            raise
        if "/balance" in page.url:
            log("[+] 已经登录")
            return
        if not wait_cloudflare(page):
            raise CloudflareBlocked("credit 登录页被 Cloudflare 拦截。")
        raise NeedManual("credit 登录页没有渲染出登录按钮，页面可能改版。")

    terms = page.query_selector("#terms")
    if terms:
        checked = page.eval_on_selector(
            "#terms", "e => e.getAttribute('aria-checked') === 'true'"
        )
        if not checked:
            terms.click()
            page.wait_for_timeout(300)
            log("[+] 已勾选服务条款")

    button = page.query_selector("button:has-text('使用 LINUX DO 登录')")
    if not button:
        button = page.query_selector("button:has-text('LINUX DO')")
    if not button:
        raise NeedManual("登录页没有找到「使用 LINUX DO 登录」按钮，页面可能已改版。")
    log("[*] 点击「使用 LINUX DO 登录」…")
    button.click()

    target = wait_for_login_or_authorize(page, LOGIN_TIMEOUT)
    if target is None:
        if not wait_cloudflare(page):
            raise CloudflareBlocked("点击登录后 Cloudflare 没有放行。")
        raise NeedManual(f"点击登录后没有跳转，当前地址：{page.url}")

    if target == "login":
        do_linuxdo_login(page, username, password)
        if "connect.linux.do/oauth2/authorize" in page.url:
            handle_authorize(page)
        else:
            wait_for_url_any(
                page,
                (
                    "connect.linux.do/oauth2/authorize",
                    "credit.linux.do/login?code=",
                    "credit.linux.do/balance",
                ),
                LOGIN_TIMEOUT,
            )
            if "connect.linux.do/oauth2/authorize" in page.url:
                handle_authorize(page)
    elif target == "authorize":
        handle_authorize(page)

    finish_oauth(page)
    log("[+] 登录完成")


# ---------------------------------------------------------------------------
# cookie 导入（备用）
# ---------------------------------------------------------------------------

def parse_cookie_payload(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        result = []
        for part in text.replace("\n", ";").split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                result.append({"name": name.strip(), "value": value.strip()})
        return result

    if isinstance(data, list):
        return [
            item
            for item in data
            if isinstance(item, dict) and item.get("name") and "value" in item
        ]
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            return [
                item
                for item in data["cookies"]
                if isinstance(item, dict) and item.get("name") and "value" in item
            ]
        return [
            {"name": name, "value": str(value)}
            for name, value in data.items()
            if isinstance(value, (str, int, float))
        ]
    return []


def import_cookies(ctx, paths: list[str]) -> None:
    cookies = []
    seen = set()
    for raw_path in paths:
        text = Path(raw_path).read_text(encoding="utf-8")
        for item in parse_cookie_payload(text):
            name = str(item.get("name", "")).strip()
            if name not in COOKIE_NAMES or name in seen:
                continue
            seen.add(name)
            domain = str(item.get("domain") or ".linux.do")
            cookie = {
                "name": name,
                "value": str(item.get("value", "")),
                "domain": domain,
                "path": str(item.get("path") or "/"),
                "secure": True,
                "httpOnly": bool(item.get("httpOnly", False)),
                "sameSite": "Lax",
            }
            expires = item.get("expires") or item.get("expirationDate")
            if expires:
                try:
                    cookie["expires"] = float(expires)
                except (TypeError, ValueError):
                    pass
            cookies.append(cookie)

    if not cookies:
        raise NeedManual("导入的文件里没有找到可用的 linux.do cookie。")

    ctx.add_cookies(cookies)
    log(f"[+] 已导入 {len(cookies)} 个 cookie")
    for cookie in cookies:
        log(f"    {cookie['name']:<28} domain={cookie['domain']}")
    if "auth.session-token" not in seen:
        log("[!] 没有 auth.session-token：可以查余额，但 credit 会话过期后需要重新登录。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def clear_credit_session(ctx) -> None:
    for cookie in ctx.cookies():
        if cookie["name"] == "linux_do_credit_session_id":
            ctx.clear_cookies(
                name=cookie["name"],
                domain=cookie["domain"],
                path=cookie.get("path", "/"),
            )
            log("[*] 已清除旧 credit 会话")


def print_user(user: dict) -> None:
    log("[+] 当前会话有效")
    log(f"[+] 用户：{user.get('nickname')} (@{user.get('username')})")
    log(f"[+] 可用余额：{user.get('available_balance')} LDC")
    log(f"[+] 社区积分：{user.get('community_balance')}")
    log(f"[+] 待结算：{user.get('pending_balance')} LDC")


# ---------------------------------------------------------------------------
# 飞书通知（Webhook 优先，未配置/失败时回退青龙 notify.py）
# ---------------------------------------------------------------------------

def _feishu_payload(text: str) -> dict:
    payload = {"msg_type": "text", "content": {"text": text}}
    secret = os.getenv("CREDIT_WEBHOOK_SECRET", "").strip()
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(digest).decode("utf-8")
    return payload


def try_webhook(text: str) -> str:
    """返回 success / failed / ambiguous / unavailable。"""
    url = os.getenv("CREDIT_WEBHOOK_URL", "").strip()
    if not url:
        return "unavailable"
    if not url.lower().startswith(("http://", "https://")) or any(
        ch.isspace() for ch in url
    ):
        log("[!] CREDIT_WEBHOOK_URL 格式非法（只填 https:// 开头的完整 URL）")
        return "failed"

    proxy = (
        os.getenv("CREDIT_WEBHOOK_PROXY")
        or os.getenv("CREDIT_BROWSER_PROXY")
        or os.getenv("PROXY_URL")
    )
    if proxy and proxy.lower().startswith("socks"):
        log("[!] 飞书通知不支持 SOCKS 代理，将直连")
        proxy = ""

    data = json.dumps(_feishu_payload(text), ensure_ascii=False).encode("utf-8")
    handlers = []
    if proxy:
        handlers.append(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            result = json.loads(body)
            ok = result.get("code") == 0 or result.get("StatusCode") == 0
        except Exception:
            ok = False
        if ok:
            return "success"
        log(f"[!] 飞书 Webhook 返回异常：{body[:200]}")
        return "failed"
    except urllib.error.HTTPError as exc:
        log(f"[!] 飞书 Webhook HTTP {exc.code}")
        return "failed"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        log("[!] 飞书 Webhook 网络错误，结果不确定")
        return "ambiguous"
    except Exception as exc:  # noqa: BLE001
        log(f"[!] 飞书 Webhook 发送失败：{type(exc).__name__}")
        return "failed"


def try_qinglong_notify(title: str, text: str) -> bool:
    for path in ("/ql/data/scripts", "/ql/scripts"):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        import notify  # type: ignore
    except Exception:
        return False
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            notify.send(title, text)
    except Exception as exc:  # noqa: BLE001
        output = buffer.getvalue()
        if output:
            print(output, end="", flush=True)
        log(f"[!] 青龙 notify.py 发送失败：{type(exc).__name__}")
        return False
    output = buffer.getvalue()
    if output:
        print(output, end="", flush=True)
    lowered = output.lower()
    if any(
        keyword in output or keyword in lowered
        for keyword in (
            "无推送渠道",
            "请检查通知变量",
            "未配置",
            "not configured",
            "no push channel",
        )
    ):
        return False
    return True


def send_notification(title: str, lines: list[str]) -> None:
    body = "\n".join(lines)
    result = try_webhook(f"{title}\n{body}")
    if result == "success":
        log("[+] 飞书通知已发送（Webhook）")
        return
    if result == "unavailable":
        log("[*] 未配置 CREDIT_WEBHOOK_URL，尝试青龙 notify.py")
    elif result == "ambiguous":
        log("[!] Webhook 结果不确定，继续尝试青龙 notify.py（可能重复）")
    else:
        log("[*] Webhook 未成功，尝试青龙 notify.py")

    if try_qinglong_notify(title, body):
        log("[+] 飞书通知已发送（青龙 notify.py）")
    else:
        log("[!] 未发送飞书通知；可在青龙环境变量配置 CREDIT_WEBHOOK_URL")


def _sanitize_message(message: str) -> str:
    # 去掉 URL 查询串，并屏蔽可能出现的 cookie / OAuth code，避免发到通知里。
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?…", str(message))
    text = re.sub(
        r"(?i)(linux_do_credit_session_id|cf_clearance|auth\.session-token|"
        r"_forum_session|_t)=([^;\s&]+)",
        r"\1=***",
        text,
    )
    text = re.sub(r"(?i)([?&](?:code|state)=)[^&\s]+", r"\1***", text)
    return text[:500]


def notify_success(user: dict) -> None:
    send_notification(
        "LINUX DO Credit 余额",
        [
            f"用户：{user.get('nickname')} (@{user.get('username')})",
            f"可用余额：{user.get('available_balance')} LDC",
            f"社区积分：{user.get('community_balance')}",
            f"待结算：{user.get('pending_balance')} LDC",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        ],
    )


def notify_failure(message: str) -> None:
    if os.getenv("CREDIT_NOTIFY_ON_ERROR", "1").lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    send_notification(
        "LINUX DO Credit 任务失败",
        [
            _sanitize_message(message),
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        ],
    )


def run(args) -> int:
    username = os.getenv("LINUXDO_USERNAME", "").strip()
    password = os.getenv("LINUXDO_PASSWORD", "")

    if args.profile_dir:
        profile_dir = Path(args.profile_dir).expanduser().resolve()
    else:
        profile_dir = default_profile_dir()
    cdp_url = start_chromium(profile_dir)

    with contextlib.ExitStack() as stack:
        try:
            session = stack.enter_context(BrowserSession(cdp_url))
        except Exception as exc:  # noqa: BLE001
            log(f"[!] 连接 Chromium 失败：{exc}")
            log("[*] 清理旧浏览器并重新启动 …")
            close_browser(profile_dir)
            cdp_url = start_chromium(profile_dir)
            session = stack.enter_context(BrowserSession(cdp_url))

        page = session.page
        if args.import_cookies:
            import_cookies(session.ctx, args.import_cookies)
        if args.login:
            clear_credit_session(session.ctx)

        user = None
        if not args.login:
            user = check_session(page)

        if user is None:
            if args.check:
                raise NeedManual("当前未登录；--check 模式不会自动登录。")
            login(page, username, password)
            user = check_session(page)

        if not user:
            raise NeedManual("登录流程结束，但 credit 会话仍然无效。")
        print_user(user)
        notify_success(user)
        return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LINUX DO Credit 浏览器自动登录（青龙）"
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="强制清除 credit 会话并重新登录",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查会话，不自动登录",
    )
    parser.add_argument(
        "--import-cookies",
        nargs="+",
        metavar="FILE",
        help="导入 Cookie-Editor 导出的 JSON（可传多个文件）",
    )
    parser.add_argument(
        "--profile-dir",
        metavar="DIR",
        help="浏览器 profile 目录",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="停止由脚本启动的 Chromium/Xvfb",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="输出调试信息（不会输出密码）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    for key in ("NO_PROXY", "no_proxy"):
        parts = [p for p in os.environ.get(key, "").split(",") if p]
        for host in ("127.0.0.1", "localhost"):
            if host not in parts:
                parts.append(host)
        os.environ[key] = ",".join(parts)

    args = parse_args(argv)
    if args.debug:
        os.environ["DEBUG"] = "1"

    try:
        if args.close:
            close_browser()
            return 0
        lock = acquire_lock()  # noqa: F841  保持到进程结束
        return run(args)
    except MissingDependency as exc:
        log(f"[x] {exc}")
        notify_failure(str(exc))
        return 4
    except CloudflareBlocked as exc:
        log(f"[x] {exc}")
        log("    处理：在浏览器里手动完成验证；或从 Mac 导出 cookie 后用 --import-cookies 导入。")
        notify_failure(
            f"{exc}；请在浏览器里手动完成验证，或导入 cookie 后重试。"
        )
        return 3
    except (NeedManual, LoginFailed) as exc:
        log(f"[x] {exc}")
        notify_failure(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        log(f"[x] 运行失败：{exc}")
        notify_failure(f"运行失败：{exc}")
        if os.getenv("DEBUG"):
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
