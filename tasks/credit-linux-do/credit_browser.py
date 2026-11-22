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

CF_TITLES = (
    "请稍候…",
    "请稍候...",
    "Just a moment...",
    "Just a moment…",
    "Attention Required! | Cloudflare",
    "Attention Required!",
    "Sorry, you have been blocked",
)
CF_BLOCK_MARKERS = ("attention required", "you have been blocked", "访问被拒绝")

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


def display_usable(display: str) -> bool:
    """Linux 下检查本地 DISPLAY 是否真的可用；macOS 直接信任。"""
    if not sys.platform.startswith("linux"):
        return True
    if not display.startswith(":"):
        return True
    number = display[1:].split(".")[0]
    socket_file = Path(f"/tmp/.X11-unix/X{number}")
    if not socket_file.exists():
        return False

    # 优先实际探测 X server；socket 文件可能只是上次崩溃留下的。
    for tool, args in (
        ("xdpyinfo", ["-display", display]),
        ("xset", ["-display", display, "q"]),
    ):
        exe = shutil.which(tool)
        if exe:
            try:
                result = subprocess.run(
                    [exe] + args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                return result.returncode == 0
            except Exception:
                return False

    # 没有探测工具时，至少确认 xvfb.pid 里的进程还活着；
    # 没有 pid 文件就无法确认，保守起见另起一个 Xvfb。
    try:
        pid = int((runtime_dir() / "xvfb.pid").read_text().strip())
        return "Xvfb" in pid_cmdline(pid)
    except (OSError, ValueError):
        return False


def ensure_display() -> Optional[subprocess.Popen]:
    """没有 DISPLAY 时启动 Xvfb，返回 Xvfb 进程（已有 DISPLAY 则返回 None）。"""
    if not sys.platform.startswith("linux"):
        # macOS 直接使用桌面会话，不需要 Xvfb。
        return None
    current = os.getenv("DISPLAY")
    if current:
        if display_usable(current):
            debug(f"使用已有 DISPLAY={current}")
            return None
        log(f"[!] DISPLAY={current} 不可用，重新启动 Xvfb")
        os.environ.pop("DISPLAY", None)

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
            if display_usable(display):
                os.environ["DISPLAY"] = display
                log(f"[*] 复用已有 Xvfb（DISPLAY={display}）")
                return None
            log(f"[!] {display} 的 X socket 已失效，跳过")
            continue
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


def cdp_targets(port: int) -> list[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def wait_for_page_targets(port: int, timeout: float = 5.0) -> list[dict]:
    """等待 Chromium 真正创建页面目标；没有页面通常意味着显示初始化失败。"""
    deadline = time.time() + timeout
    while True:
        pages = [
            target
            for target in cdp_targets(port)
            if target.get("type") == "page"
        ]
        if pages or time.time() >= deadline:
            return pages
        time.sleep(0.3)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def browser_proxy() -> Optional[str]:
    """返回 Chromium 使用的代理。

    Chromium 不会自动读取 http_proxy/https_proxy，必须通过 --proxy-server
    显式传入；这里同时兼容青龙里常见的 PROXY_URL。
    CREDIT_BROWSER_PROXY 显式设为空、none、direct、off 时强制直连。
    """
    explicit = os.getenv("CREDIT_BROWSER_PROXY")
    if explicit is not None:
        explicit = explicit.strip()
        if not explicit or explicit.lower() in ("none", "direct", "off", "0"):
            return None
        return _normalize_proxy(explicit)

    for name in (
        "PROXY_URL",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return _normalize_proxy(value)
    return None


def _normalize_proxy(proxy: str) -> str:
    # Chromium 不认 socks5h://，但 socks5:// 可以。
    if proxy.startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://") :]
    return proxy


def redact_proxy(proxy: str) -> str:
    """隐藏代理 URL 里的用户名密码，避免写进日志。"""
    try:
        normalized = proxy if "://" in proxy else f"http://{proxy}"
        parsed = parse.urlparse(normalized)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            if parsed.port:
                host += f":{parsed.port}"
            return f"{parsed.scheme}://***@{host}"
    except Exception:
        pass
    return proxy


def public_ip(proxy: Optional[str]) -> Optional[str]:
    """查询浏览器出口 IP，用于判断 cf_clearance 是否因 IP 变化而失效。"""
    if proxy and proxy.lower().startswith("socks"):
        debug("跳过出口 IP 查询：urllib 不支持 SOCKS 代理")
        return None
    if proxy:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    else:
        # 显式清空环境变量代理，和 CREDIT_BROWSER_PROXY=none 的浏览器行为一致。
        handlers = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        "https://api.ipify.org",
        headers={"User-Agent": "credit-browser/1.0"},
    )
    try:
        with opener.open(request, timeout=8) as response:
            value = response.read(64).decode("ascii", "ignore").strip()
    except Exception as exc:  # noqa: BLE001
        debug(f"查询出口 IP 失败：{type(exc).__name__}: {exc}")
        return None
    if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", value):
        return value
    return None


def check_exit_ip(proxy: Optional[str]) -> None:
    """输出出口 IP；与上次不同则提示 cf_clearance 会失效。"""
    ip = public_ip(proxy)
    if not ip:
        log("[!] 无法查询出口 IP（只影响诊断，不影响任务）")
        return
    state_path = runtime_dir() / "exit_ip"
    previous = ""
    try:
        previous = state_path.read_text().strip()
    except OSError:
        pass
    log(f"[*] 浏览器出口 IP：{ip}")
    if previous and previous != ip:
        log(
            f"[!] 出口 IP 已变化（上次 {previous}）："
            "cf_clearance 与 IP 绑定，Cloudflare 会重新挑战"
        )
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(ip)
    except OSError:
        pass


def log_cf_clearance(ctx) -> None:
    """提示当前 profile 是否已有 cf_clearance；不输出 cookie 值。"""
    try:
        cookies = ctx.cookies("https://credit.linux.do")
    except Exception:
        return
    names = {cookie.get("name") for cookie in cookies}
    if "cf_clearance" in names:
        debug("浏览器 profile 已有 cf_clearance")
    else:
        log("[*] 浏览器 profile 还没有 cf_clearance，本次会先走 Cloudflare 挑战")


def start_chromium(profile_dir: Path) -> str:
    """返回 CDP URL。浏览器已在运行则直接复用。"""
    external = os.getenv("CREDIT_CDP_URL")
    if external:
        log("[*] 使用外部 Chromium（CREDIT_CDP_URL），代理由该浏览器自己决定")
        return external.rstrip("/")

    state_path = runtime_dir() / "cdp_url"
    pid_path = runtime_dir() / "chromium.pid"
    proxy_state_path = runtime_dir() / "browser.proxy"
    proxy = browser_proxy()
    proxy_hash = hashlib.sha256((proxy or "").encode("utf-8")).hexdigest()
    if state_path.is_file():
        saved = state_path.read_text().strip()
        saved_port = parse.urlparse(saved).port
        saved_ok = False
        restart_reason = ""
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
            try:
                saved_proxy_hash = (
                    proxy_state_path.read_text().strip() or None
                )
            except OSError:
                saved_proxy_hash = None
            if saved_proxy_hash is None:
                if proxy:
                    restart_reason = "检测到新配置的浏览器代理"
                    saved_ok = False
            elif saved_proxy_hash != proxy_hash:
                restart_reason = "浏览器代理配置已变化"
                saved_ok = False
        if saved_ok:
            log(f"[*] 复用上次启动的 Chromium（CDP {saved_port}）")
            return saved
        log(f"[!] {restart_reason or '上次的 Chromium 状态无效'}，重新启动 …")
        # 先停掉可能还占着 profile 的旧进程，否则新 Chromium 会复用旧实例。
        close_browser(profile_dir, stop_xvfb=False)
        for path in (state_path, pid_path, proxy_state_path):
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
    try:
        version = subprocess.check_output(
            [chromium, "--version"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=5,
        ).strip()
    except Exception:
        version = "unknown"
    log(f"[*] Chromium 版本：{version}")
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
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--noerrdialogs",
        f"--window-size={WINDOW_SIZE}",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
        log(f"[*] Chromium 代理：{redact_proxy(proxy)}")
    else:
        log("[*] Chromium 未配置代理，直连")
    args.append(LOGIN_URL)

    log(f"[*] 启动 Chromium：{chromium}")
    debug(
        "启动参数："
        + " ".join(
            "--proxy-server=" + redact_proxy(arg.split("=", 1)[1])
            if arg.startswith("--proxy-server=")
            else arg
            for arg in args[:-1]
        )
    )
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
            proxy_state_path.write_text(proxy_hash)
            log(f"[+] Chromium 已就绪（CDP {port}）")
            pages = wait_for_page_targets(port)
            if pages:
                debug(
                    "CDP 页面："
                    + "，".join(
                        (target.get("url") or "(blank)")[:80]
                        for target in pages
                    )
                )
            else:
                log("[!] Chromium 没有页面目标，可能没有连上 Xvfb。")
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


def close_browser(
    profile_dir: Optional[Path] = None, stop_xvfb: bool = True
) -> None:
    _kill_pid_file(
        runtime_dir() / "chromium.pid",
        str(profile_dir or default_profile_dir()),
    )
    if stop_xvfb:
        _kill_pid_file(runtime_dir() / "xvfb.pid", "Xvfb")
    try:
        (runtime_dir() / "cdp_url").unlink()
    except OSError:
        pass
    try:
        (runtime_dir() / "browser.proxy").unlink()
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
    try:
        from importlib.metadata import version as _version

        log(f"[*] Playwright 版本：{_version('playwright')}")
    except Exception:
        pass
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


def save_debug_screenshot(page, tag: str) -> None:
    """Cloudflare 卡住时保存截图，方便在青龙文件管理器里查看。"""
    try:
        path = (
            runtime_dir()
            / "logs"
            / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=False)
        log(f"[*] 已保存页面截图：{path}")
    except Exception as exc:  # noqa: BLE001
        debug(f"保存截图失败：{type(exc).__name__}")


def _challenge_resources(page) -> list[dict]:
    """从 performance 里读取 Cloudflare 挑战脚本的加载情况。"""
    try:
        resources = page.evaluate(
            """() => performance.getEntriesByType('resource')
              .filter(r => /challenges\\.cloudflare\\.com|cdn-cgi\\/challenge-platform|\\/cdn-cgi\\/challenge/.test(r.name))
              .slice(-8)
              .map(r => ({
                name: r.name,
                status: (typeof r.responseStatus === 'number' ? r.responseStatus : null),
                ms: Math.round(r.duration || 0),
                size: r.transferSize || 0,
              }))"""
        )
    except Exception:
        return []
    return list(resources or [])


def cloudflare_diagnostics(page) -> None:
    """Cloudflare 超时时输出可操作信息；不读取、不输出 cookie 值。"""
    try:
        state = page.evaluate(
            """() => {
              const iframes = Array.from(document.querySelectorAll('iframe'))
                .filter(f => /challenges\\.cloudflare\\.com/.test(f.src || ''))
                .map(f => ({w: f.offsetWidth || 0, h: f.offsetHeight || 0}));
              const input = document.querySelector('input[name="cf-turnstile-response"]');
              return {
                title: document.title || '',
                url: location.href,
                turnstileIframes: iframes,
                tokenLen: input ? (input.value || '').length : -1,
                hasApi: typeof window.turnstile !== 'undefined',
                text: (document.body ? document.body.innerText : '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 200),
              };
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        debug(f"读取 Cloudflare 页面状态失败：{type(exc).__name__}")
        state = {}

    title = state.get("title") or page_title(page)
    url = state.get("url") or page.url
    log(f"[!] Cloudflare 未通过：title={title!r}，url={url}")

    iframes = state.get("turnstileIframes") or []
    token_len = int(state.get("tokenLen", -1))
    has_api = bool(state.get("hasApi", False))
    if iframes:
        visible = [
            item
            for item in iframes
            if item.get("w", 0) > 20 and item.get("h", 0) > 20
        ]
        log(
            f"[!] Turnstile iframe：{len(iframes)} 个（可见 {len(visible)} 个），"
            f"window.turnstile={'已加载' if has_api else '未加载'}"
        )
    else:
        log("[!] 页面里没有 Turnstile iframe")

    if token_len == 0:
        log("[!] Turnstile token 为空：挑战没有完成")
    elif token_len > 0:
        log(f"[!] Turnstile token 已填充（长度 {token_len}），但页面仍未跳转")

    resources = _challenge_resources(page)
    for item in resources:
        name = str(item.get("name", "")).split("?")[0]
        short = name[-90:] if len(name) > 90 else name
        status = item.get("status")
        if status is None:
            status_text = "未知"
        elif status == 0:
            status_text = "失败/被拦截"
        else:
            status_text = str(status)
        log(
            f"[!] 挑战资源：{short} status={status_text} "
            f"{item.get('ms', 0)}ms size={item.get('size', 0)}"
        )
    if not resources:
        log("[!] 没有记录到挑战脚本资源：可能被代理/DNS 拦截，或页面尚未开始挑战")

    if any(item.get("status") == 0 for item in resources):
        log("[!] 判断：挑战脚本请求失败，检查代理是否放行 challenges.cloudflare.com")
    elif iframes and token_len == 0:
        log(
            "[!] 判断：Turnstile 已加载但未自动完成；"
            "可能需要人工点击，或出口 IP 被重点挑战"
        )
    elif not iframes and not resources:
        log(
            "[!] 判断：挑战页没有加载出 Turnstile；"
            "检查代理/DNS，或把 CREDIT_CF_TIMEOUT 调大到 180 再试"
        )

    text = state.get("text") or ""
    if text:
        debug(f"Cloudflare 页面文字：{text[:160]}")

    save_debug_screenshot(page, "cf")


def wait_cloudflare(page, timeout: int = CF_TIMEOUT) -> bool:
    """等待 Cloudflare 挑战页跳走；返回是否通过。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = page_title(page)
        lowered = title.lower()
        if any(marker in lowered for marker in CF_BLOCK_MARKERS):
            log(f"[!] Cloudflare 明确拒绝访问：title={title!r}")
            cloudflare_diagnostics(page)
            return False
        if title and title not in CF_TITLES:
            return True
        debug(f"Cloudflare 等待中：title={title!r} url={page.url}")
        page.wait_for_timeout(1000)
    cloudflare_diagnostics(page)
    return False


def is_error_page(page) -> bool:
    """Chromium 网络层错误页（DNS、代理、连接失败等），不是 Cloudflare 页面。"""
    url = page.url or ""
    return url.startswith("chrome-error://") or url.startswith("edge-error://")


def page_error_detail(page) -> str:
    """提取 Chromium 错误页上的错误码/说明，便于日志定位。"""
    try:
        text = page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception:
        return ""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return " | ".join(lines[:5])[:400]


def goto(page, url: str) -> None:
    debug(f"打开 {url}")
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            message = str(exc)
            if "net::ERR_ABORTED" in message and not is_error_page(page):
                debug("导航被后续跳转中断，继续等待 …")
                break
            if "Timeout" not in type(exc).__name__:
                detail = page_error_detail(page) or message.splitlines()[0]
                if attempt == 0:
                    debug(f"打开 {url} 失败，2 秒后重试：{detail}")
                    page.wait_for_timeout(2000)
                    continue
                raise CreditError(
                    f"打开 {url} 失败：{detail}\n"
                    "这是 Chromium 网络层错误，不是 Cloudflare。请检查青龙容器的 "
                    "DNS、代理和对外网络。"
                ) from exc
            debug(f"页面加载超时：{url}")

        if is_error_page(page):
            detail = page_error_detail(page)
            if attempt == 0:
                debug(f"打开 {url} 进入 Chromium 错误页，2 秒后重试：{detail}")
                page.wait_for_timeout(2000)
                continue
            raise CreditError(
                f"打开 {url} 失败：{detail or 'Chromium 错误页'}\n"
                "这是 Chromium 网络层错误，不是 Cloudflare。请检查青龙容器的 "
                "DNS、代理和对外网络。"
            )
        break

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


def is_navigation_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "Execution context was destroyed" in message
        or "Cannot find context with specified id" in message
        or "most likely because of a navigation" in message
    )


def api_user(page) -> Optional[dict]:
    """在页面上下文里调用 user-info；返回用户数据，401 返回 None。"""
    last_exc = None
    for attempt in range(3):
        if not on_credit(page):
            detail = page_error_detail(page)
            hint = ""
            if is_error_page(page):
                hint = (
                    "\n这是 Chromium 网络层错误，不是 Cloudflare。"
                    "请检查青龙容器的 DNS、代理和对外网络。"
                )
            raise CreditError(
                f"当前页面不在 credit.linux.do：{page.url}"
                + (f"\n页面提示：{detail}" if detail else "")
                + hint
            )
        try:
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
        except Exception as exc:  # noqa: BLE001
            if not is_navigation_error(exc):
                raise
            last_exc = exc
            debug(f"user-info 页面导航中，重试 {attempt + 1}/3")
            page.wait_for_timeout(1000)
            continue

        status = int(result.get("status", 0))
        text = result.get("text", "")
        debug(f"user-info HTTP {status}")

        if status == 200:
            payload = json.loads(text)
            return payload.get("data")
        if status == 401:
            return None
        if status == 403 and ("Just a moment" in text or "challenge" in text):
            if attempt < 2:
                debug("user-info 遇到 Cloudflare 403，等待后重试")
                page.wait_for_timeout(2000)
                continue
            raise CloudflareBlocked("Cloudflare 拦截了 user-info 请求。")
        raise CreditError(f"user-info 返回 HTTP {status}：{text[:200]}")

    raise CreditError(f"页面持续导航，无法读取会话：{last_exc}")


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
        if is_error_page(page):
            detail = page_error_detail(page)
            raise CreditError(
                f"登录跳转失败：{detail or 'Chromium 错误页'}\n"
                "这是 Chromium 网络层错误，请检查代理是否允许 "
                "connect.linux.do 和 linux.do。"
            )
        url = page.url
        if "connect.linux.do/oauth2/authorize" in url:
            return "authorize"
        if "credit.linux.do/login?code=" in url or "credit.linux.do/balance" in url:
            return "credit"
        parsed = parse.urlparse(url)
        if parsed.netloc == "linux.do" and (
            parsed.path.startswith("/login")
            or parsed.path.startswith("/session/sso_provider")
        ):
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
    cloudflare_diagnostics(page)
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


def wait_credit_api_ready(page, timeout: int = CF_TIMEOUT) -> None:
    """等待 credit 的 Cloudflare 挑战结束（API 不再返回挑战页）。"""
    deadline = time.time() + timeout
    last_status = None
    last_text = ""
    while time.time() < deadline:
        try:
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
                  return {status: r.status, text: (await r.text()).slice(0, 300)};
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            if not is_navigation_error(exc):
                raise
            page.wait_for_timeout(1000)
            continue

        status = int(result.get("status", 0))
        last_status = status
        last_text = str(result.get("text", ""))
        if status in (200, 401):
            debug(f"credit API 已就绪（HTTP {status}）")
            return
        challenge = status == 403 and (
            "Just a moment" in last_text
            or "challenge" in last_text.lower()
            or "<html" in last_text.lower()
        )
        if challenge:
            debug("credit API 仍在 Cloudflare 挑战中，等待 …")
            page.wait_for_timeout(2000)
            continue
        raise CreditError(
            f"credit API 返回 HTTP {status}：{last_text[:200]}"
        )

    raise CloudflareBlocked(
        f"Cloudflare 挑战没有通过（credit API 持续返回 HTTP {last_status}）。"
        "脚本不会绕过验证；请检查代理出口 IP，或稍后重试。"
    )


def terms_checked(page) -> bool:
    try:
        return bool(
            page.eval_on_selector(
                "#terms",
                "e => e.getAttribute('aria-checked') === 'true'"
                " || e.getAttribute('data-state') === 'checked'",
            )
        )
    except Exception:
        return False


def oauth_login_url(page) -> str:
    """直接调用登录按钮背后的 OAuth 接口，返回 connect.linux.do 授权地址。"""
    result = None
    last_exc = None
    for attempt in range(3):
        try:
            result = page.evaluate(
                """async () => {
                  const r = await fetch('/api/v1/oauth/login', {
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
            break
        except Exception as exc:  # noqa: BLE001
            if not is_navigation_error(exc):
                raise
            last_exc = exc
            debug(f"OAuth 接口页面导航中，重试 {attempt + 1}/3")
            page.wait_for_timeout(1000)
    if result is None:
        raise CreditError(f"调用 OAuth 登录接口时页面持续导航：{last_exc}")

    status = int(result.get("status", 0))
    text = str(result.get("text", ""))
    if status != 200:
        hint = ""
        if status == 403:
            hint = (
                "\n这是 Cloudflare 拦截，不是账号密码错误。"
                "请检查代理出口 IP，或稍后重试。"
            )
        raise CreditError(
            f"/api/v1/oauth/login 返回 HTTP {status}：{text[:300]}{hint}"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CreditError(
            f"/api/v1/oauth/login 返回非 JSON：{text[:300]}"
        ) from exc
    url = payload.get("data")
    if not isinstance(url, str) or not url.startswith(
        "https://connect.linux.do/oauth2/authorize"
    ):
        raise CreditError(f"/api/v1/oauth/login 返回异常：{text[:300]}")
    return url


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

    # 页面渲染不代表 Cloudflare 已经放行 API；先等 API 可访问再点登录。
    wait_credit_api_ready(page)
    if "/balance" in page.url:
        log("[+] 已经登录")
        return

    terms = page.query_selector("#terms")
    if terms:
        if terms_checked(page):
            log("[+] 服务条款已勾选")
        else:
            try:
                terms.click()
            except Exception as exc:  # noqa: BLE001
                debug(f"点击服务条款失败：{type(exc).__name__}")
            page.wait_for_timeout(300)
            if not terms_checked(page):
                try:
                    terms.evaluate("e => e.click()")
                except Exception:
                    pass
                page.wait_for_timeout(300)
            if terms_checked(page):
                log("[+] 已勾选服务条款")
            else:
                raise NeedManual(
                    "服务条款勾选失败，登录按钮不会生效。"
                    "请检查页面是否被遮挡或已改版。"
                )
    else:
        log("[!] 没有找到服务条款复选框，继续尝试登录 …")

    button = page.query_selector("button:has-text('使用 LINUX DO 登录')")
    if not button:
        button = page.query_selector("button:has-text('LINUX DO')")
    if not button:
        raise NeedManual("登录页没有找到「使用 LINUX DO 登录」按钮，页面可能已改版。")
    try:
        enabled = button.is_enabled()
    except Exception:
        enabled = True
    if not enabled:
        raise NeedManual(
            "「使用 LINUX DO 登录」按钮是禁用状态，可能服务条款没有勾选成功。"
        )
    log("[*] 点击「使用 LINUX DO 登录」…")
    failed_requests = []

    def _on_request_failed(request):
        parsed = parse.urlparse(request.url)
        if parsed.netloc.lower() in ("linux.do", "connect.linux.do"):
            failed_requests.append(
                f"{parsed.netloc}{parsed.path}：{request.failure or 'unknown'}"
            )

    page.on("requestfailed", _on_request_failed)
    try:
        button.click()

        target = wait_for_login_or_authorize(page, 20)
        if target is None and not on_credit(page):
            # 点击可能已经触发跳转，只是还没提交；再给一点时间。
            target = wait_for_login_or_authorize(page, 10)
        if target is None:
            # 按钮的点击事件偶尔不触发跳转（前端报错/被代理拦截）。
            # 直接调用同一个 OAuth 接口，拿到授权地址后继续，流程不变。
            log("[!] 按钮点击后没有跳转，尝试直接调用 OAuth 登录接口 …")
            oauth_url = oauth_login_url(page)
            debug(f"OAuth 地址：{oauth_url[:120]}…")
            goto(page, oauth_url)
            target = wait_for_login_or_authorize(page, LOGIN_TIMEOUT)
    finally:
        try:
            page.remove_listener("requestfailed", _on_request_failed)
        except Exception:
            pass

    if target is None:
        if not wait_cloudflare(page):
            raise CloudflareBlocked("点击登录后 Cloudflare 没有放行。")
        extra = ""
        if failed_requests:
            extra = "\n失败的请求：" + "；".join(failed_requests[-5:])
        raise NeedManual(
            f"点击登录后没有跳转，当前地址：{page.url}{extra}"
        )

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

    proxy = os.getenv("CREDIT_WEBHOOK_PROXY", "").strip()
    if not proxy:
        proxy = browser_proxy() or ""
    if proxy.lower() in ("none", "direct", "off", "0"):
        proxy = ""
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
    if os.getenv("CREDIT_CDP_URL"):
        log("[*] 使用外部 Chromium，跳过出口 IP 检查")
    else:
        check_exit_ip(browser_proxy())

    with contextlib.ExitStack() as stack:
        try:
            session = stack.enter_context(BrowserSession(cdp_url))
        except Exception as exc:  # noqa: BLE001
            log(f"[!] 连接 Chromium 失败：{exc}")
            log("[*] 清理旧浏览器和 Xvfb，重新启动 …")
            close_browser(profile_dir, stop_xvfb=True)
            os.environ.pop("DISPLAY", None)
            cdp_url = start_chromium(profile_dir)
            session = stack.enter_context(BrowserSession(cdp_url))

        page = session.page
        log_cf_clearance(session.ctx)
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
