#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机 Chrome 导出 credit.linux.do 登录 cookie。

用途
----
青龙容器里的 Chromium 登录 linux.do 时可能被 hCaptcha 拦住。可以在已经登录的
Mac/PC Chrome 上导出会话 cookie，再用 credit_browser.py --import-cookies 导入
青龙，跳过 linux.do 登录。

为什么不能用一个网页读取
------------------------
`_forum_session`、`_t`、`auth.session-token` 都是 HttpOnly cookie，
JavaScript 的 `document.cookie` 读不到；只能通过 DevTools、浏览器扩展或
Chrome DevTools Protocol（本脚本用的方式）读取。

前提
----
Chrome 以调试端口运行，并且已经登录 https://credit.linux.do/balance：

    "/path/to/Google Chrome" \
      --remote-debugging-port=9222 \
      --user-data-dir="$HOME/.credit_profile" \
      "https://credit.linux.do/balance"

用法
----
    python3 export_cookies.py
    python3 export_cookies.py --out /tmp/credit_cookies.json
    python3 export_cookies.py --include-cf-clearance

输出是 Cookie-Editor 兼容的 JSON。脚本只在本机读取 cookie，不发送到网络。
输出文件权限为 600；它等同于登录态，不要提交 Git、不要分享。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib import request

DEFAULT_CDP = os.getenv("CREDIT_CDP_URL", "http://127.0.0.1:9222")
URLS = [
    "https://credit.linux.do",
    "https://linux.do",
    "https://connect.linux.do",
]

# 容器里真正需要的 cookie。cf_clearance 默认不导出：它和本机 IP、
# User-Agent 绑定，到青龙里通常会失效，容器会自己重新过 CF。
CORE = {"linux_do_credit_session_id", "auth.session-token"}
EXTRA = {"_forum_session", "_t", "sso_payload", "_cfuvid", "__cf_ob"}
OPTIONAL = {"cf_clearance"}


def cdp_alive(cdp_url: str) -> bool:
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(
            cdp_url.rstrip("/") + "/json/version", timeout=3
        ) as response:
            return response.status == 200
    except Exception:
        return False


def read_cookies(cdp_url: str, include_cf: bool) -> tuple[list[dict], set[str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "缺少 playwright。请在 Mac 上执行：\n"
            "  python3 -m pip install playwright\n"
            "不需要 playwright install。"
        )

    wanted = CORE | EXTRA | (OPTIONAL if include_cf else set())
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise SystemExit("Chrome 没有可用的浏览器上下文。")
        raw = browser.contexts[0].cookies(URLS)

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
        raise SystemExit(
            "没有找到 linux_do_credit_session_id。请确认 Chrome 已经登录 "
            "https://credit.linux.do/balance，而不是只打开了登录页。"
        )
    return cookies, seen


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从本机 Chrome 导出 credit.linux.do 登录 cookie"
    )
    parser.add_argument(
        "--cdp",
        default=DEFAULT_CDP,
        help=f"Chrome CDP 地址，默认 {DEFAULT_CDP}",
    )
    parser.add_argument(
        "--out",
        default="credit_cookies.json",
        help="输出文件，默认 ./credit_cookies.json",
    )
    parser.add_argument(
        "--include-cf-clearance",
        action="store_true",
        help="同时导出 cf_clearance（跨 IP/UA 通常会失效）",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="只把 JSON 输出到 stdout（方便粘贴到青龙 CREDIT_COOKIE_JSON）",
    )
    args = parser.parse_args()

    if not cdp_alive(args.cdp):
        print(f"[x] 连不上 Chrome CDP：{args.cdp}", file=sys.stderr)
        print("    请先用调试端口启动 Chrome（路径以你机器为准）：", file=sys.stderr)
        print(
            '    "/path/to/Google Chrome" --remote-debugging-port=9222 '
            '--user-data-dir="$HOME/.credit_profile" '
            '"https://credit.linux.do/balance"',
            file=sys.stderr,
        )
        return 1

    cookies, seen = read_cookies(args.cdp, args.include_cf_clearance)
    payload = json.dumps(cookies, ensure_ascii=False, indent=2)

    if args.stdout:
        print(payload)
        print(
            "[!] 以上内容包含登录凭据，粘贴到青龙 CREDIT_COOKIE_JSON 后"
            "请清空终端记录。",
            file=sys.stderr,
        )
        return 0

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8")
    os.chmod(out, 0o600)

    print(f"[+] 已导出 {len(cookies)} 个 cookie -> {out}")
    for item in cookies:
        expires = item.get("expires")
        if expires:
            days = max(0, round((expires - time.time()) / 86400))
            lifetime = f"{days} 天"
        else:
            lifetime = "会话"
        print(f"    {item['name']:<30} domain={item['domain']:<20} {lifetime}")

    if "auth.session-token" not in seen:
        print(
            "[!] 没有 auth.session-token：可以查余额，但 credit 会话过期后"
            "无法自动续期。"
        )
    missing_extra = EXTRA - seen
    if missing_extra:
        print(
            "[!] 没找到（通常不影响）："
            + "、".join(sorted(missing_extra))
        )

    print("[!] 这个文件等同于登录态：不要提交 Git、不要发到群里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
