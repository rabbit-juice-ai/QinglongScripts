#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机 Chrome 导出 credit.linux.do 登录 cookie（命令行版）。

用途
----
青龙容器里的 Chromium 登录 linux.do 时可能被 hCaptcha 拦住。可以在已经登录的
Mac/PC Chrome 上导出会话 cookie，再放到青龙环境变量 `CREDIT_COOKIE_JSON`，
跳过 linux.do 登录。

为什么不能用一个网页读取
------------------------
`_forum_session`、`_t`、`auth.session-token` 都是 HttpOnly cookie，
JavaScript 的 `document.cookie` 读不到；只能通过 DevTools、浏览器扩展或
Chrome DevTools Protocol（本脚本用的方式）读取。想要页面操作，请用同目录的
`cookie_tool.py`。

用法
----
    python3 export_cookies.py
    python3 export_cookies.py --stdout
    python3 export_cookies.py --launch
    python3 export_cookies.py --out /tmp/credit_cookies.json
    python3 export_cookies.py --include-cf-clearance

输出是 Cookie-Editor 兼容的 JSON。脚本只在本机读取 cookie，不发送到网络。
输出文件权限为 600；它等同于登录态，不要提交 Git、不要分享。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cookie_core import (
    DEFAULT_CDP,
    DEFAULT_PROFILE,
    EXTRA,
    ToolError,
    cdp_alive,
    check_linuxdo_login,
    describe_expiry,
    payload_json,
    read_cookies,
)


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
    parser.add_argument(
        "--launch",
        action="store_true",
        help="CDP 没运行时，自动启动一个带调试端口的 Chrome",
    )
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help=f"自动启动 Chrome 时使用的 profile，默认 {DEFAULT_PROFILE}",
    )
    parser.add_argument(
        "--chrome",
        default=None,
        help="Chrome 可执行文件路径；默认自动查找",
    )
    parser.add_argument(
        "--no-check-linuxdo",
        action="store_true",
        help="不检查 linux.do 登录态（更快，但可能导出匿名 auth.session-token）",
    )
    args = parser.parse_args()

    try:
        if not cdp_alive(args.cdp):
            if args.launch:
                from cookie_core import launch_chrome

                print(f"[*] CDP 未运行，启动 Chrome（profile：{args.profile}）…")
                launch_chrome(
                    args.cdp,
                    profile=args.profile,
                    chrome=args.chrome,
                )
            else:
                print(f"[x] 连不上 Chrome CDP：{args.cdp}", file=sys.stderr)
                print(
                    "    请先用调试端口启动 Chrome，或加上 --launch 自动启动：",
                    file=sys.stderr,
                )
                print(
                    '    python3 export_cookies.py --launch',
                    file=sys.stderr,
                )
                return 1

        cookies, seen = read_cookies(args.cdp, args.include_cf_clearance)
        linuxdo = (
            None if args.no_check_linuxdo else check_linuxdo_login(args.cdp)
        )
    except ToolError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 1

    linuxdo_warning = None
    if linuxdo and linuxdo.get("ok") is False:
        linuxdo_warning = (
            "[!] linux.do 未登录：auth.session-token 可能只是匿名会话，"
            "导入后无法自动续期。\n"
            "    请先在 Chrome 打开 https://linux.do 登录，再重新导出。"
        )
    elif linuxdo and linuxdo.get("ok") is None:
        linuxdo_warning = (
            "[!] 无法确认 linux.do 登录态："
            + str(linuxdo.get("reason") or "")
        )

    payload = payload_json(cookies)

    if args.stdout:
        print(payload)
        if linuxdo_warning:
            print(linuxdo_warning, file=sys.stderr)
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
        print(
            f"    {item['name']:<30} "
            f"domain={item['domain']:<20} {describe_expiry(item)}"
        )

    if "auth.session-token" not in seen:
        print(
            "[!] 没有 auth.session-token：可以查余额，但 credit 会话过期后"
            "无法自动续期。"
        )
    if linuxdo_warning:
        print(linuxdo_warning)
    missing_extra = EXTRA - seen
    if missing_extra:
        print("[!] 没找到（通常不影响）：" + "、".join(sorted(missing_extra)))

    print("[!] 这个文件等同于登录态：不要提交 Git、不要发到群里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
