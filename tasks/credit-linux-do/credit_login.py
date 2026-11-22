#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LINUX DO Credit 青龙自动登录 / 会话续期（单文件，纯标准库）。

站点：https://credit.linux.do

原理
----
credit.linux.do 用 LINUX DO Connect (OAuth) 登录，登录成功后种下
`linux_do_credit_session_id`（约 7 天）。本脚本只需要 3 个 cookie：

    linux_do_credit_session_id   domain=.linux.do          credit 会话
    cf_clearance                 domain=.linux.do          Cloudflare 通行证
    auth.session-token           domain=connect.linux.do   Connect 会话

每次运行：
  1. 调 /api/v1/oauth/user-info 验证当前会话；
  2. 401 时用 Connect 会话自动走一遍 OAuth（login -> authorize -> approve ->
     callback），换回新的 credit 会话；
  3. 新会话写回状态文件，下次直接复用。

限制
----
* Cloudflare 的 cf_clearance 与出口 IP 绑定。青龙（NAS）和导出 cookie
  的电脑必须走同一个公网出口（一般同一个家庭宽带即可）。
* Connect 的 auth.session-token 是服务端会话，失效后无法纯 HTTP 续期，
  需要重新从浏览器导出一次 cookie（脚本会明确提示）。
* 脚本不绕过任何人机验证；Turnstile 需要你本人在浏览器里点一次。

用法
----
    python3 credit_login.py

环境变量：
    CREDIT_COOKIE         name=value; ... 形式的 cookie（可选）
    CREDIT_COOKIE_JSON    JSON 数组/对象形式的 cookie（可选）
    CREDIT_COOKIE_FILE    状态文件路径（默认 /ql/data/credit-linux-do/credit_cookies.json，
                          本地运行且 /ql/data 不可写时回退到脚本同目录）
    CREDIT_FORCE_ENV      设为 1 时忽略状态文件，强制用环境变量
    CREDIT_UA             覆盖 User-Agent（默认与导出时的 Chrome 一致）
    DEBUG                 设为 1 时输出 OAuth 调试信息
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
from pathlib import Path
from urllib import error, parse, request

BASE = "https://credit.linux.do"
CONNECT = "https://connect.linux.do"

# 必须和导出 cookie 时的浏览器一致（cf_clearance 可能校验 UA）
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/157.0.0.0 Safari/537.36"
)

REQUIRED = {"linux_do_credit_session_id", "cf_clearance"}
REFRESH_REQUIRED = {"auth.session-token"}
ALL_COOKIES = REQUIRED | REFRESH_REQUIRED

TIMEOUT = 25


def default_state_file() -> Path:
    ql_data = Path("/ql/data")
    if ql_data.is_dir() and os.access(ql_data, os.W_OK):
        return ql_data / "credit-linux-do" / "credit_cookies.json"
    return Path(__file__).resolve().with_name("credit_cookies.json")


STATE_FILE = Path(os.getenv("CREDIT_COOKIE_FILE") or default_state_file())


class NeedManual(Exception):
    """需要人工重新导出 cookie。"""


class CloudflareBlocked(Exception):
    """cf_clearance 失效或出口 IP 不一致。"""


# ---------------------------------------------------------------------------
# cookie
# ---------------------------------------------------------------------------

def cookie_domain(name: str) -> str:
    return "connect.linux.do" if name == "auth.session-token" else ".linux.do"


def parse_cookie_string(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def load_cookies() -> dict[str, str]:
    """返回 name -> value。状态文件优先，可用 CREDIT_FORCE_ENV=1 覆盖。"""
    force_env = os.getenv("CREDIT_FORCE_ENV", "").lower() in ("1", "true", "yes")
    if not force_env and STATE_FILE.is_file():
        try:
            items = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(items, list):
                return {c["name"]: c["value"] for c in items if c.get("name")}
            if isinstance(items, dict):
                return {k: str(v) for k, v in items.items()}
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 读取 {STATE_FILE} 失败：{exc}")

    raw = (os.getenv("CREDIT_COOKIE_JSON") or os.getenv("CREDIT_COOKIE", "")).strip()
    if not raw:
        raise NeedManual(
            "没有找到 cookie。请设置环境变量 CREDIT_COOKIE（name=value; ...），"
            f"或把导出的 JSON 放到 {STATE_FILE}"
        )
    if raw.startswith("["):
        items = json.loads(raw)
        return {c["name"]: c["value"] for c in items if c.get("name")}
    if raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data.get("cookies"), list):
            return {c["name"]: c["value"] for c in data["cookies"] if c.get("name")}
        return {k: str(v) for k, v in data.items()}
    return parse_cookie_string(raw)


def make_cookie(name: str, value: str, domain: str, path: str = "/", expires=None):
    expires_int = int(expires) if expires and float(expires) > 0 else None
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=True,
        expires=expires_int,
        discard=expires_int is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


def build_opener(cookies: dict[str, str]):
    missing = REQUIRED - set(cookies)
    if missing:
        raise NeedManual(f"cookie 不完整，缺少：{', '.join(sorted(missing))}")

    jar = http.cookiejar.CookieJar()
    for name, value in cookies.items():
        if name in ALL_COOKIES:
            jar.set_cookie(make_cookie(name, value, cookie_domain(name)))
    opener = request.build_opener(request.HTTPCookieProcessor(jar))
    return opener, jar


def save_state(jar: http.cookiejar.CookieJar) -> None:
    items = []
    for cookie in jar:
        if "linux.do" not in cookie.domain or cookie.name not in ALL_COOKIES:
            continue
        items.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "expires": cookie.expires,
            }
        )
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(STATE_FILE, 0o600)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def base_headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("CREDIT_UA", DEFAULT_UA),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def http_call(opener, url: str, *, method: str = "GET", headers=None, data=None):
    merged = base_headers()
    merged.update(headers or {})
    req = request.Request(url, data=data, headers=merged, method=method)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return (
                resp.status,
                resp.geturl(),
                resp.headers,
                resp.read().decode("utf-8", "replace"),
            )
    except error.HTTPError as exc:
        return (
            exc.code,
            exc.geturl(),
            exc.headers,
            exc.read().decode("utf-8", "replace"),
        )


def is_challenge(status: int, headers, text: str) -> bool:
    return status == 403 and (
        "Just a moment" in text or headers.get("cf-mitigated") == "challenge"
    )


def check_cloudflare(status: int, headers, text: str) -> None:
    if is_challenge(status, headers, text):
        raise CloudflareBlocked(
            "Cloudflare 拦截（403）。cf_clearance 失效，或青龙的出口 IP 与导出 cookie 的电脑不一致。"
        )


def fetch_user(opener) -> dict | None:
    status, _, headers, text = http_call(
        opener,
        f"{BASE}/api/v1/oauth/user-info",
        headers={"Accept": "application/json, text/plain, */*"},
    )
    check_cloudflare(status, headers, text)
    if status == 401:
        return None
    if status >= 400:
        raise RuntimeError(f"user-info 返回 HTTP {status}：{text[:200]}")
    payload = json.loads(text)
    if payload.get("error_msg"):
        raise RuntimeError(f"user-info 返回错误：{payload['error_msg']}")
    return payload.get("data")


# ---------------------------------------------------------------------------
# OAuth 续期
# ---------------------------------------------------------------------------

def oauth_refresh(opener, jar: http.cookiejar.CookieJar) -> None:
    if not any(c.name == "auth.session-token" for c in jar):
        raise NeedManual(
            "缺少 auth.session-token，无法自动续期。请重新从浏览器导出 cookie。"
        )

    # 站点在 /login?code=... 时如果发现已有会话 cookie，就不会下发新 cookie
    # （即使旧会话已经失效）。所以续期前先清掉旧的 credit 会话。
    for cookie in list(jar):
        if cookie.name == "linux_do_credit_session_id":
            jar.clear(cookie.domain, cookie.path, cookie.name)

    # 让 credit 服务端自己生成 state/nonce，并把它记在服务端；
    # 自己拼一个随机 state 会被回调接口判为「非法登录请求」。
    status, _, headers, text = http_call(
        opener,
        f"{BASE}/api/v1/oauth/login",
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    check_cloudflare(status, headers, text)
    if status >= 400:
        raise NeedManual(f"获取登录地址失败：HTTP {status} {text[:200]}")
    auth_url = json.loads(text).get("data")
    if not auth_url:
        raise NeedManual(f"获取登录地址失败：{text[:200]}")

    no_redirect = request.build_opener(
        request.HTTPCookieProcessor(jar), NoRedirect()
    )
    status, final_url, headers, text = http_call(
        no_redirect,
        auth_url,
        headers={"Referer": f"{BASE}/login"},
    )
    check_cloudflare(status, headers, text)

    if status in (301, 302, 303, 307, 308):
        location = headers.get("Location", "")
        if "sso_provider" in location:
            raise NeedManual(
                "LINUX DO Connect 会话已失效。请重新从浏览器导出一次 cookie。"
            )
        status, final_url, headers, text = http_call(
            opener,
            parse.urljoin(CONNECT, location),
            headers={"Referer": auth_url},
        )
        check_cloudflare(status, headers, text)

    if status != 200:
        raise NeedManual(f"授权页返回 HTTP {status}，无法自动续期。")

    match = re.search(r"/oauth2/approve/[A-Za-z0-9_-]+", text)
    if not match:
        raise NeedManual(
            "授权页没有找到「允许」链接（可能触发风控或页面改版），请重新导出 cookie。"
        )

    status, final_url, headers, text = http_call(
        opener,
        parse.urljoin(CONNECT, match.group(0)),
        headers={"Referer": auth_url},
    )
    check_cloudflare(status, headers, text)
    if os.getenv("DEBUG"):
        print(f"    DEBUG 授权回调：HTTP {status} {final_url[:160]}")
        print(
            "    DEBUG jar: "
            + ", ".join(
                f"{c.domain}:{c.name}(len={len(c.value)})" for c in jar
            )
        )
    if "credit.linux.do/login" not in final_url:
        raise NeedManual(f"授权回调异常，最终地址：{final_url}")

    # 前端拿到 code 后还会 POST /api/v1/oauth/callback 换会话，
    # 这一步才会真正下发 linux_do_credit_session_id。
    query = parse.parse_qs(parse.urlparse(final_url).query)
    code = (query.get("code") or [None])[0]
    state = (query.get("state") or [None])[0]
    if not code or not state:
        raise NeedManual(f"回调地址里没有 code/state：{final_url}")

    body = json.dumps({"state": state, "code": code}).encode("utf-8")
    status, _, headers, text = http_call(
        opener,
        f"{BASE}/api/v1/oauth/callback",
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": final_url,
            "Content-Type": "application/json",
        },
        data=body,
    )
    check_cloudflare(status, headers, text)
    if status >= 400:
        raise NeedManual(f"兑换会话失败：HTTP {status} {text[:200]}")


# ---------------------------------------------------------------------------
# 导入 cookie（给其他使用者用）
# ---------------------------------------------------------------------------

def parse_cookie_payload(text: str) -> list[dict]:
    """接受 Cookie-Editor JSON 数组、{cookies:[...]}、{name:value} 或 name=value 串。"""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [
            {"name": name, "value": value}
            for name, value in parse_cookie_string(text).items()
        ]

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


def import_cookies(paths: list[str]) -> None:
    specs: dict[str, dict] = {}
    for raw_path in paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8")
        for item in parse_cookie_payload(text):
            name = str(item.get("name", "")).strip()
            if name not in ALL_COOKIES:
                continue
            specs[name] = {
                "name": name,
                "value": str(item.get("value", "")),
                "domain": cookie_domain(name),
                "path": "/",
                "expires": item.get("expires") or item.get("expirationDate"),
            }

    missing = REQUIRED - set(specs)
    if missing:
        raise NeedManual(
            "导入的文件里缺少："
            + ", ".join(sorted(missing))
            + "。请确认已在浏览器登录 credit.linux.do 后再导出。"
        )

    jar = http.cookiejar.CookieJar()
    for spec in specs.values():
        jar.set_cookie(
            make_cookie(
                spec["name"],
                spec["value"],
                spec["domain"],
                spec["path"],
                spec["expires"],
            )
        )
    save_state(jar)

    print(f"[+] 已导入 {len(specs)} 个 cookie 到 {STATE_FILE}")
    for name in sorted(specs):
        print(
            f"    {name:<28} domain={specs[name]['domain']:<18} "
            f"len={len(specs[name]['value'])}"
        )
    if "auth.session-token" not in specs:
        print("[!] 没有 auth.session-token：只能查余额，credit 会话过期后需要重新导入。")
        print("    获取方式：登录后打开 https://connect.linux.do，再用 Cookie-Editor 导出一次，")
        print("    把两个 JSON 文件一起传给 --import-cookies。")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LINUX DO Credit 自动登录 / 会话续期（青龙）"
    )
    parser.add_argument(
        "--import-cookies",
        nargs="+",
        metavar="FILE",
        help="从 Cookie-Editor 导出的 JSON 文件导入 cookie（可传多个文件）",
    )
    parser.add_argument(
        "--state-file",
        metavar="FILE",
        help="状态文件路径（默认 /ql/data/credit-linux-do/credit_cookies.json）",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    global STATE_FILE
    args = parse_args(argv)
    if args.state_file:
        STATE_FILE = Path(args.state_file)

    if args.import_cookies:
        try:
            import_cookies(args.import_cookies)
            return 0
        except NeedManual as exc:
            print(f"[x] {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"[x] 导入失败：{exc}")
            return 1

    try:
        opener, jar = build_opener(load_cookies())
        user = fetch_user(opener)

        if user is None:
            print("[*] credit 会话已过期，正在用 LINUX DO Connect 会话自动续期 …")
            oauth_refresh(opener, jar)
            user = fetch_user(opener)
            if user is None:
                raise NeedManual("OAuth 流程完成，但 credit 会话仍然无效。")
            save_state(jar)
            print(f"[+] 自动续期成功，新会话已写入 {STATE_FILE}")
        else:
            print("[+] 当前会话有效")

        print(f"[+] 用户：{user.get('nickname')} (@{user.get('username')})")
        print(f"[+] 可用余额：{user.get('available_balance')} LDC")
        print(f"[+] 社区积分：{user.get('community_balance')}")
        print(f"[+] 待结算：{user.get('pending_balance')} LDC")
        return 0

    except CloudflareBlocked as exc:
        print(f"[x] {exc}")
        print("    处理：在导出 cookie 的电脑上重新登录一次，再重新导出。")
        return 3
    except NeedManual as exc:
        print(f"[x] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 运行失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
