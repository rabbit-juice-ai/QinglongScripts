#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Credit Cookie 工具的共享逻辑。

通过 Chrome DevTools Protocol 读取 credit.linux.do 的登录 cookie。
`_forum_session`、`_t`、`auth.session-token` 都是 HttpOnly cookie，
网页里的 `document.cookie` 读不到，所以必须走 CDP。

这个模块不包含任何账号密码，也不会把 cookie 值写到日志。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib import request
from urllib.parse import quote, urlparse

DEFAULT_CDP = os.getenv("CREDIT_CDP_URL", "http://127.0.0.1:9222")
DEFAULT_PROFILE = Path(
    os.getenv("CREDIT_PROFILE_DIR", str(Path.home() / ".credit_profile"))
).expanduser()
BALANCE_URL = "https://credit.linux.do/balance"

URLS = [
    "https://credit.linux.do",
    "https://linux.do",
    "https://connect.linux.do",
]

# 青龙里真正需要的 cookie。cf_clearance 默认不导出：它和本机 IP、
# User-Agent 绑定，到容器里通常会失效，浏览器会自己重新过 CF。
CORE = {"linux_do_credit_session_id", "auth.session-token"}
EXTRA = {"_forum_session", "_t", "sso_payload", "_cfuvid", "__cf_ob"}
OPTIONAL = {"cf_clearance"}

ALLOWED_HOSTS = {"credit.linux.do", "linux.do", "connect.linux.do"}

_USER_INFO_JS = """async () => {
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


class ToolError(RuntimeError):
    """工具可以直接展示给用户的错误。"""


# ---------------------------------------------------------------------------
# CDP 基础
# ---------------------------------------------------------------------------

def _no_proxy_opener():
    return request.build_opener(request.ProxyHandler({}))


def cdp_alive(cdp_url: str = DEFAULT_CDP, timeout: float = 3) -> bool:
    try:
        with _no_proxy_opener().open(
            cdp_url.rstrip("/") + "/json/version", timeout=timeout
        ) as response:
            return response.status == 200
    except Exception:
        return False


def cdp_version(cdp_url: str = DEFAULT_CDP) -> dict:
    try:
        with _no_proxy_opener().open(
            cdp_url.rstrip("/") + "/json/version", timeout=3
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ToolError(f"连不上 Chrome CDP：{cdp_url}（{exc}）")


def page_targets(cdp_url: str = DEFAULT_CDP) -> list[dict]:
    """读取 Chrome 当前标签页，不需要 Playwright。"""
    try:
        with _no_proxy_opener().open(
            cdp_url.rstrip("/") + "/json/list", timeout=3
        ) as response:
            targets = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    pages = []
    for target in targets:
        if target.get("type") != "page":
            continue
        pages.append(
            {
                "title": target.get("title", ""),
                "url": target.get("url", ""),
            }
        )
    return pages


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ToolError(
            "缺少 playwright。请先安装：\n"
            "  python3 -m pip install playwright\n"
            "不需要执行 playwright install。"
        )
    return sync_playwright


def ensure_page_target(
    cdp_url: str = DEFAULT_CDP,
    url: str = "about:blank",
    force: bool = False,
) -> bool:
    """Chrome 没有页面时，Playwright 无法建立 context；用 /json/new 建一个。"""
    if not force and page_targets(cdp_url):
        return False
    endpoint = cdp_url.rstrip("/") + "/json/new?" + quote(url, safe=":/?=&")
    try:
        req = request.Request(endpoint, method="PUT")
        with _no_proxy_opener().open(req, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def _connect(pw, cdp_url: str):
    ensure_page_target(cdp_url)
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        if "Browser context management is not supported" not in str(exc):
            raise ToolError(
                f"连接 Chrome CDP 失败：{cdp_url}\n{exc}\n"
                "请确认 Chrome 是用 --remote-debugging-port 启动的。"
            )
        ensure_page_target(cdp_url, force=True)
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
        except Exception as retry_exc:
            raise ToolError(
                f"连接 Chrome CDP 失败：{cdp_url}\n{retry_exc}\n"
                "Chrome 可能已经没有可用窗口。请完全退出 Chrome，"
                "再重新启动调试实例。"
            )
    if not browser.contexts:
        raise ToolError("Chrome 没有可用的浏览器上下文。")
    return browser.contexts[0]


# ---------------------------------------------------------------------------
# Cookie 读取
# ---------------------------------------------------------------------------

def read_cookies(
    cdp_url: str = DEFAULT_CDP, include_cf: bool = False
) -> tuple[list[dict], set[str]]:
    """读取并规范化 cookie，返回 (cookies, 名称集合)。"""
    wanted = CORE | EXTRA | (OPTIONAL if include_cf else set())
    sync_playwright = _playwright()
    with sync_playwright() as pw:
        ctx = _connect(pw, cdp_url)
        raw = ctx.cookies(URLS)

    seen: set[str] = set()
    cookies: list[dict] = []
    for cookie in raw:
        name = cookie.get("name", "")
        if name not in wanted or name in seen:
            continue
        seen.add(name)
        item = {
            "name": name,
            "value": cookie.get("value", ""),
            "domain": cookie.get("domain", ""),
            "path": cookie.get("path", "/"),
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", True)),
            "sameSite": cookie.get("sameSite", "Lax"),
        }
        expires = cookie.get("expires", -1)
        if expires and expires > 0:
            item["expires"] = expires
        cookies.append(item)

    if "linux_do_credit_session_id" not in seen:
        raise ToolError(
            "没有找到 linux_do_credit_session_id。请确认 Chrome 已经登录 "
            "https://credit.linux.do/balance，而不是只打开了登录页。"
        )
    return cookies, seen


def payload_json(cookies: list[dict]) -> str:
    return json.dumps(cookies, ensure_ascii=False, indent=2)


def describe_expiry(cookie: dict) -> str:
    expires = cookie.get("expires") or -1
    if expires < 0:
        return "会话"
    remaining = expires - time.time()
    if remaining <= 0:
        return "已过期"
    if remaining >= 86400:
        return f"{max(1, round(remaining / 86400))} 天"
    return f"{max(1, round(remaining / 3600))} 小时"


def cookie_warnings(seen: set[str]) -> list[str]:
    warnings = []
    if "auth.session-token" not in seen:
        warnings.append(
            "没有 auth.session-token：可以查余额，但 credit 会话过期后无法自动续期。"
        )
    missing_extra = EXTRA - seen
    if missing_extra:
        warnings.append(
            "没找到（通常不影响）：" + "、".join(sorted(missing_extra))
        )
    return warnings


# ---------------------------------------------------------------------------
# Chrome 启动 / 页面操作
# ---------------------------------------------------------------------------

def _mac_chrome_paths() -> list[str]:
    paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(
            Path.home()
            / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        ),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    volumes = Path("/Volumes")
    if volumes.is_dir():
        try:
            for volume in sorted(volumes.iterdir()):
                paths.append(
                    str(
                        volume
                        / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                    )
                )
        except OSError:
            pass
    return paths


def find_chrome() -> str | None:
    env_path = os.getenv("CREDIT_CHROME_BIN", "").strip()
    if env_path:
        return env_path if Path(env_path).is_file() else None

    if sys.platform == "darwin":
        for path in _mac_chrome_paths():
            if Path(path).is_file():
                return path
        return None

    if sys.platform.startswith("win"):
        candidates = []
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.getenv(env_name)
            if base:
                candidates.append(
                    str(
                        Path(base)
                        / "Google/Chrome/Application/chrome.exe"
                    )
                )
        for path in candidates:
            if Path(path).is_file():
                return path
        return None

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def launch_chrome(
    cdp_url: str = DEFAULT_CDP,
    profile: str | Path = DEFAULT_PROFILE,
    url: str = BALANCE_URL,
    chrome: str | None = None,
    wait: float = 20,
) -> dict:
    """启动一个带调试端口的独立 Chrome；已在运行则直接返回。"""
    if cdp_alive(cdp_url):
        if page_targets(cdp_url):
            return {"launched": False, "reason": "CDP 已在运行"}
        if ensure_page_target(cdp_url, url):
            return {"launched": False, "reason": "CDP 已在运行，已打开新页面"}
        raise ToolError(
            "Chrome 的 CDP 在运行，但没有任何页面，也无法创建新页面。\n"
            "请完全退出 Chrome 后重试，或重启这个调试实例。"
        )

    parsed = urlparse(cdp_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ToolError(f"CDP 地址不是本机，无法自动启动：{cdp_url}")

    binary = chrome or find_chrome()
    if not binary:
        raise ToolError(
            "找不到 Chrome。请用 --chrome 指定路径，"
            "或设置环境变量 CREDIT_CHROME_BIN。"
        )

    profile_path = Path(profile).expanduser()
    profile_path.mkdir(parents=True, exist_ok=True)

    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1200,900",
        url,
    ]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + wait
    while time.time() < deadline:
        if cdp_alive(cdp_url, timeout=1):
            return {
                "launched": True,
                "chrome": binary,
                "profile": str(profile_path),
            }
        time.sleep(0.5)

    raise ToolError(
        f"Chrome 已启动，但 {wait:g} 秒内没有等到 CDP {cdp_url}。\n"
        f"如果 profile（{profile_path}）已经被普通 Chrome 占用，"
        "请先完全退出那个 Chrome，或换一个 profile。"
    )


def open_url(cdp_url: str = DEFAULT_CDP, url: str = BALANCE_URL) -> dict:
    """在 Chrome 里打开一个受信任的页面，返回当前地址和标题。"""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ToolError(
            "只允许打开 credit.linux.do / linux.do / connect.linux.do。"
        )

    sync_playwright = _playwright()
    with sync_playwright() as pw:
        ctx = _connect(pw, cdp_url)
        page = next(
            (p for p in ctx.pages if parsed.hostname in p.url),
            None,
        )
        if page is None:
            page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise ToolError(f"打开 {url} 失败：{exc}")
        return {"url": page.url, "title": page.title()}


def check_login(cdp_url: str = DEFAULT_CDP, timeout: float = 60) -> dict:
    """在 Chrome 里打开 /balance 并调用 user-info，确认会话是否有效。"""
    sync_playwright = _playwright()
    with sync_playwright() as pw:
        ctx = _connect(pw, cdp_url)
        page = next(
            (p for p in ctx.pages if "credit.linux.do" in p.url),
            None,
        )
        created_tab = page is None
        if page is None:
            page = ctx.new_page()

        try:
            page.goto(BALANCE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise ToolError(f"打开 {BALANCE_URL} 失败：{exc}")

        deadline = time.time() + timeout
        last = "等待 Cloudflare 超时"
        while time.time() < deadline:
            try:
                title = (page.title() or "").strip()
            except Exception:
                page.wait_for_timeout(1000)
                continue
            if any(
                marker in title
                for marker in (
                    "Just a moment",
                    "请稍候",
                    "Attention Required",
                    "Sorry, you have been blocked",
                )
            ):
                last = f"Cloudflare 挑战未完成（页面标题：{title}）"
                page.wait_for_timeout(2000)
                continue

            try:
                result = page.evaluate(_USER_INFO_JS)
            except Exception as exc:  # 页面跳转中
                last = f"读取 user-info 失败：{exc}"
                page.wait_for_timeout(1500)
                continue

            status = int(result.get("status") or 0)
            text = result.get("text") or ""
            if status == 200:
                try:
                    data = json.loads(text).get("data") or {}
                except json.JSONDecodeError:
                    raise ToolError("user-info 返回了无法解析的 JSON。")
                return {
                    "ok": True,
                    "user": data,
                    "url": page.url,
                    "created_tab": created_tab,
                }
            if status == 401:
                return {
                    "ok": False,
                    "reason": "credit 会话无效或已过期（HTTP 401）",
                    "url": page.url,
                    "created_tab": created_tab,
                }
            if status == 403:
                last = "Cloudflare 拦截了 user-info（HTTP 403）"
            else:
                last = f"user-info HTTP {status}：{' '.join(text.split())[:120]}"
            page.wait_for_timeout(2000)

        return {
            "ok": False,
            "reason": last,
            "url": page.url,
            "created_tab": created_tab,
        }
