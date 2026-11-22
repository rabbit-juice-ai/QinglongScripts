#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Credit Cookie 页面工具。

在本机启动一个小页面（默认 http://127.0.0.1:8765/），用页面按钮完成：

1. 启动一个带调试端口的 Chrome；
2. 打开 credit.linux.do 登录页；
3. 读取登录 cookie，生成青龙环境变量 CREDIT_COOKIE_JSON；
4. 验证当前会话是否有效（调用 user-info）。

页面只监听 127.0.0.1，不上传任何数据；但生成的 JSON 等同于登录态，
不要截图或分享。

用法
----
    python3 cookie_tool.py
    python3 cookie_tool.py --port 8766
    python3 cookie_tool.py --no-launch --no-open
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from cookie_core import (
    BALANCE_URL,
    DEFAULT_CDP,
    DEFAULT_PROFILE,
    ToolError,
    cdp_alive,
    cdp_version,
    check_linuxdo_login,
    check_login,
    cookie_warnings,
    describe_expiry,
    find_chrome,
    launch_chrome,
    open_url,
    page_targets,
    payload_json,
    read_cookies,
)

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

# Playwright 的同步 API 不适合并发调用；所有浏览器操作串行执行。
BROWSER_LOCK = threading.Lock()

CONFIG = {
    "cdp": DEFAULT_CDP,
    "profile": str(DEFAULT_PROFILE),
    "chrome": None,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CreditCookieTool/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础响应 ---------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {self.command} {self.path}\n"
        )

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json(404, {"ok": False, "error": "文件不存在"})
            return
        body = path.read_bytes()
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_port
        return origin in {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ToolError("请求体太大。")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ToolError("请求不是有效的 JSON。")
        if not isinstance(data, dict):
            raise ToolError("请求必须是 JSON 对象。")
        return data

    def _handle(self, action) -> None:
        if not self._origin_ok():
            self._send_json(403, {"ok": False, "error": "拒绝跨站请求"})
            return
        try:
            self._send_json(200, action())
        except ToolError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                500, {"ok": False, "error": f"内部错误：{exc}"}
            )

    # -- 路由 -------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._handle(self._status)
            return
        self._send_json(404, {"ok": False, "error": "未知接口"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        routes = {
            "/api/cookies": self._cookies,
            "/api/launch": self._launch,
            "/api/open": self._open,
            "/api/check": self._check,
        }
        action = routes.get(path)
        if action is None:
            self._send_json(404, {"ok": False, "error": "未知接口"})
            return
        self._handle(action)

    # -- 接口实现 ---------------------------------------------------------

    def _status(self) -> dict:
        cdp = CONFIG["cdp"]
        alive = cdp_alive(cdp)
        version = None
        pages: list[dict] = []
        if alive:
            try:
                info = cdp_version(cdp)
                version = {
                    "browser": info.get("Browser", ""),
                    "protocol": info.get("Protocol-Version", ""),
                }
            except ToolError:
                version = None
            pages = page_targets(cdp)
        return {
            "ok": True,
            "cdp": alive,
            "cdp_url": cdp,
            "version": version,
            "pages": pages,
            "chrome": CONFIG["chrome"],
            "profile": CONFIG["profile"],
        }

    def _cookies(self) -> dict:
        data = self._read_json()
        include_cf = bool(data.get("include_cf_clearance"))
        with BROWSER_LOCK:
            cookies, seen = read_cookies(CONFIG["cdp"], include_cf)
            linuxdo = check_linuxdo_login(CONFIG["cdp"])

        warnings = cookie_warnings(seen)
        if linuxdo.get("ok") is False:
            warnings.append(
                "linux.do 未登录：auth.session-token 可能只是匿名会话，导入后"
                "无法自动续期。请先在 Chrome 打开 https://linux.do 登录，"
                "再重新读取。"
            )
        elif linuxdo.get("ok") is None:
            warnings.append(
                "无法确认 linux.do 登录态：" + str(linuxdo.get("reason") or "")
            )
        if include_cf:
            warnings.append(
                "cf_clearance 与出口 IP、User-Agent 绑定，导入青龙后通常无效。"
            )
        return {
            "ok": True,
            "count": len(cookies),
            "cookies": [
                {
                    "name": item["name"],
                    "domain": item["domain"],
                    "path": item["path"],
                    "expiry": describe_expiry(item),
                    "httpOnly": item["httpOnly"],
                    "secure": item["secure"],
                }
                for item in cookies
            ],
            "payload": payload_json(cookies),
            "warnings": warnings,
            "linuxdo": linuxdo,
        }

    def _launch(self) -> dict:
        # 页面会带一个空的 JSON body；不读掉会残留在 keep-alive 连接里，
        # 导致下一次请求被解析成 "{}GET ..."。
        self._read_json()
        with BROWSER_LOCK:
            result = launch_chrome(
                CONFIG["cdp"],
                profile=CONFIG["profile"],
                chrome=CONFIG["chrome"],
            )
        result["ok"] = True
        return result

    def _open(self) -> dict:
        data = self._read_json()
        url = str(data.get("url") or BALANCE_URL)
        with BROWSER_LOCK:
            result = open_url(CONFIG["cdp"], url)
        result["ok"] = True
        return result

    def _check(self) -> dict:
        data = self._read_json()
        try:
            timeout = float(data.get("timeout") or 60)
        except (TypeError, ValueError):
            timeout = 60
        timeout = max(10, min(timeout, 180))
        with BROWSER_LOCK:
            result = check_login(CONFIG["cdp"], timeout=timeout)
        result["ok"] = bool(result.get("ok"))
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="启动 Credit Cookie 页面工具"
    )
    parser.add_argument("--port", type=int, default=8765, help="页面端口，默认 8765")
    parser.add_argument("--cdp", default=DEFAULT_CDP, help="Chrome CDP 地址")
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help="自动启动 Chrome 时使用的 profile",
    )
    parser.add_argument("--chrome", default=None, help="Chrome 可执行文件路径")
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="启动时不自动拉起 Chrome",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="不自动打开浏览器页面",
    )
    args = parser.parse_args()

    CONFIG["cdp"] = args.cdp
    CONFIG["profile"] = str(Path(args.profile).expanduser())
    CONFIG["chrome"] = args.chrome or find_chrome()

    if not args.no_launch and not cdp_alive(args.cdp):
        print("[*] 未检测到 Chrome CDP，尝试启动 Chrome …")
        try:
            launch_chrome(
                args.cdp,
                profile=args.profile,
                chrome=args.chrome,
            )
        except ToolError as exc:
            print(f"[!] 自动启动 Chrome 失败：{exc}")
            print("[!] 仍会打开页面；可以在页面里重试，或手动启动 Chrome。")
        else:
            print("[+] Chrome 已启动")

    host = "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, args.port), Handler)
    except OSError as exc:
        print(f"[x] 无法监听 {host}:{args.port}：{exc}", file=sys.stderr)
        print("    换一个端口：python3 cookie_tool.py --port 8766", file=sys.stderr)
        return 1
    server.daemon_threads = True

    url = f"http://{host}:{args.port}/"
    print(f"[+] Credit Cookie 工具：{url}")
    print(f"[*] CDP：{args.cdp}")
    print(f"[*] Profile：{CONFIG['profile']}")
    print("[!] 页面生成的 JSON 等同于登录态，不要截图或分享。")
    print("[*] 按 Ctrl+C 停止。")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
