#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentRouter 青龙签到脚本（单文件，纯标准库 HTTP 实现）。

- 账号配置：环境变量 AGENTROUTER_ACCOUNTS（JSON 数组）
- 调度：青龙定时任务直接执行本文件
- 结果：标准输出（青龙任务日志）+ 飞书汇总通知
- 无数据库、无 Web、无第三方依赖；默认串行执行
- 遇到 Turnstile/CAPTCHA/MFA 不绕过，暂停该账号并记录原因

用法：
    python3 agentrouter_checkin.py --mode full
    python3 agentrouter_checkin.py --mode single --account <username>
    python3 agentrouter_checkin.py --mode dry-run
    python3 agentrouter_checkin.py --mode site-connectivity
    python3 agentrouter_checkin.py --mode feishu-connectivity
    python3 agentrouter_checkin.py --mode session-check --account <username>
"""

import argparse
import contextlib
import fcntl
import hashlib
import http.cookiejar
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://agentrouter.org"
DEFAULT_DATA_DIR = "/ql/data/agentrouter-checkin"

MAX_ACCOUNTS = 20
REQUEST_TIMEOUT_SECONDS = 30
ACCOUNT_BUDGET_SECONDS = 420
RUN_BUDGET_SECONDS = 30 * 60
MIN_ACCOUNT_START_BUDGET_SECONDS = 240

TITLE = "AgentRouter 每日签到与余额报告"
ALLOWED_FIELDS = {"username", "password", "enabled", "expected_name"}
HUMAN_VERIFY_KEYWORDS = (
    "turnstile", "captcha", "verify", "verification", "mfa", "otp",
    "验证码", "人机", "安全验证", "二次验证",
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message):
    print(message, flush=True)


def _parse_json(text):
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _sanitize(text, secrets):
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:300]


def _safe_exc(exc, secrets):
    return _sanitize(f"{type(exc).__name__}: {exc}", secrets)


def _to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _identity_matches(configured, data):
    """配置值必须与返回的 username 或 email 之一一致（忽略大小写与首尾空白）。

    站点登录框接受“用户名或邮箱”；若配置的是邮箱，返回的 username 通常是
    账号用户名而非邮箱，因此需要同时比对 email 字段。
    """
    configured_norm = configured.strip().casefold()
    if not configured_norm:
        return False
    for key in ("username", "email"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            if value.strip().casefold() == configured_norm:
                return True
    return False


def _mask(value):
    text = str(value or "").strip()
    if not text:
        return "空"
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "***" + text[-2:]


def _identity_hint(data):
    """脱敏诊断：只暴露掩码后的 username 与 email 是否存在。"""
    has_email = bool(str(data.get("email") or "").strip())
    return f"返回 username={_mask(data.get('username'))}，email字段={'有' if has_email else '无'}"


def _ok(resp):
    return bool(resp and resp.json_data and resp.json_data.get("success"))


def _data(resp):
    if resp and resp.json_data and isinstance(resp.json_data.get("data"), dict):
        return resp.json_data["data"]
    return {}


def _message(resp):
    if resp and resp.json_data and isinstance(resp.json_data.get("message"), str):
        return resp.json_data["message"]
    return ""


# ---------------------------------------------------------------------------
# HTTP 客户端（每账号独立 Cookie 会话文件）
# ---------------------------------------------------------------------------

class ApiResponse:
    def __init__(self, status, json_data, text):
        self.status = status
        self.json_data = json_data
        self.text = text


def _session_filename(username):
    """会话文件以 username 为键：清洗特殊字符 + 短哈希防冲突/防路径问题。"""
    normalized = (username or "").strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^a-z0-9_-]+", "_", normalized)[:40].strip("_") or "account"
    return f"{safe}_{digest}.cookies"


def _ssl_context():
    """可选：AGENTROUTER_TLS_MAX=1.2 时限制最高 TLS 1.2（排查中间设备干扰 TLS 1.3）。"""
    context = ssl.create_default_context()
    if _env("AGENTROUTER_TLS_MAX").lower() in ("1.2", "tls1.2"):
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


class HttpClient:
    """每个账号一个实例：独立 CookieJar，按 username 持久化到 data/sessions/。"""

    def __init__(self, session_key, base_url, proxy_url, data_dir):
        self.account_id = session_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.user_id = None
        session_dir = os.path.join(data_dir, "sessions")
        os.makedirs(session_dir, mode=0o700, exist_ok=True)
        self.cookie_path = os.path.join(session_dir, _session_filename(session_key))
        self.meta_path = self.cookie_path[:-len(".cookies")] + ".json"
        self.jar = http.cookiejar.LWPCookieJar(self.cookie_path)
        if os.path.exists(self.cookie_path):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                # 损坏的会话文件按"无会话"处理
                self.jar = http.cookiejar.LWPCookieJar(self.cookie_path)
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
                saved_uid = meta.get("user_id")
                if saved_uid is not None:
                    self.user_id = saved_uid
            except Exception:
                pass
        handlers = [
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        ]
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        self.opener = urllib.request.build_opener(*handlers)

    def _request(self, method, path, payload=None, omit_user_header=False):
        url = self.base_url + path
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AgentRouter-Checkin/1.0)",
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": self.base_url + "/login",
        }
        if not omit_user_header:
            headers["New-API-User"] = str(self.user_id) if self.user_id is not None else "-1"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                text = resp.read().decode("utf-8", "replace")
                return ApiResponse(resp.status, _parse_json(text), text)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return ApiResponse(exc.code, _parse_json(text), text)

    def get(self, path, omit_user_header=False, retries=2):
        """GET 请求带有限重试（仅幂等请求；登录 POST 不重试）。"""
        last_exc = None
        for attempt in range(retries + 1):
            try:
                return self._request("GET", path, omit_user_header=omit_user_header)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc

    def post_json(self, path, payload):
        return self._request("POST", path, payload)

    def save_session(self):
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
            os.chmod(self.cookie_path, 0o600)
        except Exception:
            pass
        try:
            with open(self.meta_path, "w", encoding="utf-8") as handle:
                json.dump({"user_id": self.user_id}, handle)
            os.chmod(self.meta_path, 0o600)
        except Exception:
            pass


def api_status(client):
    return client.get("/api/status")


def api_self(client):
    return client.get("/api/user/self")


def api_logout(client):
    return client.get("/api/user/logout")


def api_login(client, username, password):
    # 与站点前端一致：turnstile 参数为空（未启用时）；启用 Turnstile 时本脚本不尝试登录
    return client.post_json("/api/user/login?turnstile=", {"username": username, "password": password})


# ---------------------------------------------------------------------------
# 账号配置
# ---------------------------------------------------------------------------

def load_accounts():
    """解析并校验 AGENTROUTER_ACCOUNTS。返回 (accounts, errors)。"""
    raw = os.environ.get("AGENTROUTER_ACCOUNTS", "")
    if not raw.strip():
        return [], ["ACCOUNTS_ENV_MISSING: 未设置 AGENTROUTER_ACCOUNTS"]
    try:
        data = json.loads(raw)
    except Exception:
        return [], ["ACCOUNTS_JSON_INVALID: 不是合法 JSON"]
    if not isinstance(data, list):
        return [], ["ACCOUNTS_JSON_INVALID: 必须是 JSON 数组"]
    if not (1 <= len(data) <= MAX_ACCOUNTS):
        return [], [f"ACCOUNTS_COUNT_OUT_OF_RANGE: 账号数量 {len(data)} 不在 1~{MAX_ACCOUNTS}"]

    errors = []
    accounts = []
    seen_usernames = {}
    for index, item in enumerate(data):
        prefix = f"第 {index + 1} 个账号"
        item_errors_before = len(errors)
        if not isinstance(item, dict):
            errors.append(f"{prefix}: 必须是对象")
            continue
        extra = set(item) - ALLOWED_FIELDS
        if extra:
            errors.append(f"{prefix}: 未知字段 {sorted(extra)}")
        username = item.get("username")
        password = item.get("password")
        enabled = item.get("enabled")
        expected_name = item.get("expected_name")

        if not isinstance(username, str) or not username.strip():
            errors.append(f"{prefix}: username 为空或不是字符串")
        else:
            key = username.strip().casefold()
            if key in seen_usernames:
                errors.append(
                    f"{prefix}: username 重复（与第 {seen_usernames[key] + 1} 个账号冲突）"
                )
            else:
                seen_usernames[key] = index
        if not isinstance(password, str) or password == "":
            errors.append(f"{prefix}: password 为空或不是字符串")
        if not isinstance(enabled, bool):
            errors.append(f"{prefix}: enabled 必须是布尔值 true/false")
        if expected_name is not None and (
            not isinstance(expected_name, str) or not expected_name.strip()
        ):
            errors.append(f"{prefix}: expected_name 必须是非空字符串（可省略）")

        if len(errors) == item_errors_before and isinstance(username, str):
            accounts.append({
                "username": username.strip(),
                "password": password,
                "enabled": enabled,
                "expected_name": expected_name.strip() if isinstance(expected_name, str) else None,
            })
    if errors:
        return [], errors
    return accounts, []


# ---------------------------------------------------------------------------
# 单账号处理
# ---------------------------------------------------------------------------

def _result(account_id):
    return {
        "id": account_id,
        "display": account_id,
        "status": "failed",
        "login": "skipped",
        "checkin_raw": None,
        "before": None,
        "after": None,
        "change": None,
        "quota_per_unit": None,
        "error": None,
        "detail": "",
    }


def _not_executed(account_id, reason):
    result = _result(account_id)
    result.update(status="not_executed", error=reason, detail="本次未执行")
    return result


def process_account(account, base_url, proxy_url, data_dir):
    """处理单个账号；任何异常都收敛为该账号失败，不影响其他账号。"""
    username = account["username"]
    account_id = username
    expected = account.get("expected_name")
    secrets = [username, account["password"]]
    if expected:
        secrets.append(expected)
    result = _result(account_id)
    result["display"] = username
    client = None
    try:
        client = HttpClient(username, base_url, proxy_url, data_dir)

        # 1. 旧会话探测：配置了 expected_name 时严格比对，否则仅提示；不采用不匹配会话的余额
        probe = api_self(client)
        if probe.status == 200 and _ok(probe):
            data = _data(probe)
            probe_reference = expected or username
            if not _identity_matches(probe_reference, data):
                log(f"  注意: 已有会话账号与配置不一致（{_identity_hint(data)}），"
                    f"不采用该会话余额，直接重新登录")
                result["before"] = None
            else:
                result["before"] = _to_int(data.get("quota"))
                # 2. 退出旧会话（触发登录签到的前提），并确认退出成功
                api_logout(client)
                check = api_self(client)
                if check.status == 200 and _ok(check):
                    result.update(status="failed", error="LOGOUT_UNCERTAIN", detail="退出后会话仍然有效")
                    return result
                if check.status not in (200, 401):
                    result.update(
                        status="failed",
                        error="LOGOUT_UNCERTAIN",
                        detail=f"退出确认请求异常 HTTP {check.status}",
                    )
                    return result
                client.user_id = None
        elif probe.status == 401 or (probe.status == 200 and not _ok(probe)):
            # 会话失效：签到前余额未知，继续登录
            result["before"] = None
        else:
            result.update(
                status="failed",
                error="SESSION_PROBE_FAILED",
                detail=f"会话探测失败 HTTP {probe.status}",
            )
            return result

        # 3. 人机验证预检：站点启用 Turnstile 时纯 HTTP 无法完成，暂停该账号
        status_resp = api_status(client)
        status_data = _data(status_resp) if status_resp.status == 200 else {}
        unit = _to_int(status_data.get("quota_per_unit"))
        result["quota_per_unit"] = unit if (isinstance(unit, int) and unit > 0) else None
        if status_data.get("turnstile_check"):
            result.update(
                status="paused",
                error="HUMAN_ACTION_REQUIRED",
                detail="站点启用了 Turnstile，纯 HTTP 无法完成登录，已跳过（不绕过）",
            )
            return result

        # 4. 使用配置的用户名和密码登录
        login = api_login(client, username, account["password"])
        if login.status != 200 or not _ok(login):
            message = _sanitize(_message(login), secrets) or f"HTTP {login.status}"
            lowered = message.lower()
            if any(keyword in lowered for keyword in HUMAN_VERIFY_KEYWORDS):
                result.update(status="paused", error="HUMAN_ACTION_REQUIRED", detail=message)
            else:
                result.update(status="failed", error="LOGIN_FAILED", detail=message)
            return result

        # 登录成功即视为成功；严格名称比对放在登录后用户信息（权威来源）阶段
        login_data = _data(login)
        result["login"] = "success"
        checked_in = login_data.get("checked_in")
        result["checkin_raw"] = checked_in if isinstance(checked_in, bool) else None
        login_uid = login_data.get("id")
        site_name = login_data.get("username")
        if isinstance(site_name, str) and site_name.strip():
            result["display"] = site_name.strip()
        if not expected and not _identity_matches(username, login_data):
            log(f"  注意: 登录返回账号与配置不一致（{_identity_hint(login_data)}）；"
                f"未配置 expected_name，按登录成功继续")
        client.user_id = login_uid

        # 5. 登录后重新获取用户信息与余额
        after_resp = api_self(client)
        if after_resp.status != 200 or not _ok(after_resp):
            result.update(
                status="failed",
                error="BALANCE_FAILED",
                detail=f"登录后获取用户信息失败 HTTP {after_resp.status}",
            )
            return result
        after_data = _data(after_resp)
        after_site_name = after_data.get("username")
        if isinstance(after_site_name, str) and after_site_name.strip():
            result["display"] = after_site_name.strip()
        if expected:
            if not _identity_matches(expected, after_data):
                result.update(
                    status="failed",
                    error="IDENTITY_MISMATCH",
                    detail=f"用户信息账号与 expected_name 不一致，余额不采用（{_identity_hint(after_data)}）",
                )
                return result
        elif not _identity_matches(username, after_data):
            log(f"  注意: 用户信息账号与配置不一致（{_identity_hint(after_data)}）；"
                f"未配置 expected_name，按登录成功继续")
        after_uid = after_data.get("id")
        if login_uid is not None and after_uid is not None and str(login_uid) != str(after_uid):
            log("  注意: 登录响应与用户信息的 user id 不一致，仍按登录成功继续")

        result["after"] = _to_int(after_data.get("quota"))
        if result["before"] is not None and result["after"] is not None:
            result["change"] = result["after"] - result["before"]
        result["status"] = "ok"
        return result
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        result.update(status="failed", error="NETWORK_ERROR", detail=_safe_exc(exc, secrets))
        return result
    except Exception as exc:  # 单账号边界：任何异常都不影响其他账号
        result.update(status="failed", error="UNEXPECTED_ERROR", detail=_safe_exc(exc, secrets))
        return result
    finally:
        if client is not None:
            client.save_session()


# ---------------------------------------------------------------------------
# 汇总与输出
# ---------------------------------------------------------------------------

def _fmt_balance(value, unit):
    if not isinstance(value, int):
        return "未知"
    if isinstance(unit, int) and unit > 0:
        return f"${value / unit:,.2f}（{value} quota）"
    return f"{value} quota"


def _fmt_change(value, unit):
    if not isinstance(value, int):
        return "未知"
    if isinstance(unit, int) and unit > 0:
        amount = value / unit
        sign = "+" if amount > 0 else ("-" if amount < 0 else "")
        return f"{sign}${abs(amount):,.2f}（{value:+d} quota）"
    return f"{value:+d}"


def _login_text(result):
    return {
        "success": "成功",
        "failed": "失败",
        "skipped": "未执行",
    }.get(result["login"], result["login"])


def _checkin_text(result):
    if result["status"] == "not_executed":
        return "未执行"
    if result["login"] != "success":
        return "未执行"
    raw = result["checkin_raw"]
    if raw is True:
        return "checked_in=true（含义待真实账号验证）"
    if raw is False:
        return "checked_in=false（不解释为已签到，语义待验证）"
    return "未知（响应未提供 checked_in）"


def print_account_result(result):
    unit = result.get("quota_per_unit")
    label = result.get("display") or result["id"]
    log(f"  [{label}] 状态={result['status']} 登录={result['login']} "
        f"签到前={_fmt_balance(result['before'], unit)} 签到后={_fmt_balance(result['after'], unit)} "
        f"变化={_fmt_change(result['change'], unit)}"
        + (f" 错误={result['error']}" if result.get("error") else ""))


def build_summary(results, enabled_count, use_display=False):
    def label(result):
        if use_display:
            return result.get("display") or result["id"]
        return result["id"]

    lines = [TITLE, f"执行时间: {utc_now()}", f"启用账号数量: {enabled_count}", ""]
    for result in results:
        unit = result.get("quota_per_unit")
        lines.append(f"【{label(result)}】")
        lines.append(f"登录状态: {_login_text(result)}")
        lines.append(f"签到状态: {_checkin_text(result)}")
        lines.append(f"签到前余额: {_fmt_balance(result['before'], unit)}")
        lines.append(f"签到后余额: {_fmt_balance(result['after'], unit)}")
        lines.append(f"最新余额: {_fmt_balance(result['after'], unit)}")
        lines.append(f"余额变化: {_fmt_change(result['change'], unit)}")
        if result.get("error"):
            lines.append(f"失败原因: {result['error']} {result.get('detail', '')}".strip())
        lines.append("")

    ok_count = sum(1 for r in results if r["status"] == "ok")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    paused_count = sum(1 for r in results if r["status"] == "paused")
    skipped_count = sum(1 for r in results if r["status"] == "not_executed")
    lines.append("【执行汇总】")
    lines.append(f"账号总数: {enabled_count}")
    lines.append(f"完成: {ok_count}")
    lines.append(f"失败: {failed_count}")
    lines.append(f"需人工: {paused_count}")
    lines.append(f"未执行: {skipped_count}")

    known_pairs = [
        (r["after"], r.get("quota_per_unit"))
        for r in results
        if r["status"] == "ok" and isinstance(r["after"], int)
    ]
    raw_sum = sum(value for value, _ in known_pairs)
    units = {u for _, u in known_pairs if isinstance(u, int) and u > 0}

    def _fmt_total(raw, unit):
        if isinstance(unit, int) and unit > 0:
            return f"${raw / unit:,.2f}（{raw} quota）"
        return f"{raw} quota"

    complete = enabled_count > 0 and ok_count == enabled_count and len(known_pairs) == enabled_count
    if complete:
        if len(units) == 1:
            lines.append(f"总余额: {_fmt_total(raw_sum, next(iter(units)))}")
        elif not units:
            lines.append(f"总余额: {raw_sum} quota（未获取换算比例，未换算美元）")
        else:
            lines.append(f"总余额: {raw_sum} quota（换算比例不一致，未换算美元）")
    else:
        subtotal_unit = next(iter(units)) if len(units) == 1 else None
        lines.append(
            f"总余额: 无法计算（已确认小计 {_fmt_total(raw_sum, subtotal_unit)}，"
            f"覆盖 {len(known_pairs)}/{enabled_count}）"
        )
    return "\n".join(lines)


def _exit_code(results):
    if not results:
        return 2
    ok_count = sum(1 for r in results if r["status"] == "ok")
    if ok_count == len(results):
        return 0
    if ok_count > 0:
        return 1
    return 2


# ---------------------------------------------------------------------------
# 通知（notify.py 优先，明确失败才回退 Webhook；不确定不自动回退）
# ---------------------------------------------------------------------------

def _try_qinglong_notify(title, text):
    for path in ("/ql/data/scripts", "/ql/scripts"):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        import notify  # type: ignore
    except Exception:
        return "unavailable"
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            notify.send(title, text)
    except Exception:
        output = buffer.getvalue()
        if output:
            print(output, end="", flush=True)
        # 调用抛错时无法判定是否已实际发出，按"结果不确定"处理
        return "ambiguous"
    output = buffer.getvalue()
    if output:
        print(output, end="", flush=True)
    lowered = output.lower()
    if any(
        keyword in output or keyword in lowered
        for keyword in ("无推送渠道", "请检查通知变量", "未配置", "not configured", "no push channel")
    ):
        return "definite_failure"
    return "success"


def _strip_title(text):
    prefix = TITLE + "\n"
    return text[len(prefix):] if text.startswith(prefix) else text


def _try_webhook(url, text, proxy_url):
    if not url:
        return "unavailable"
    url = url.strip()
    if (
        not url.lower().startswith(("http://", "https://"))
        or any(ch.isspace() for ch in url)
    ):
        log("FEISHU_FAILED: AGENTROUTER_WEBHOOK_URL 格式非法（只填写 https:// 开头的完整 URL，不要包含引号、换行或 export 等文本）")
        return "definite_failure"
    payload = json.dumps({
        "msg_type": "text",
        "content": {"text": text},
    }).encode("utf-8")
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    try:
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
    except ValueError:
        log("FEISHU_FAILED: AGENTROUTER_WEBHOOK_URL 格式非法")
        return "definite_failure"
    try:
        with opener.open(request, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
            ok = data.get("code") == 0 or data.get("StatusCode") == 0
        except Exception:
            ok = False
        return "success" if ok else "definite_failure"
    except urllib.error.HTTPError:
        return "definite_failure"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        return "ambiguous"


def notify_summary(text, proxy_url, webhook_url):
    body = _strip_title(text)
    result = _try_qinglong_notify(TITLE, body)
    if result == "success":
        log("NOTIFY_OK: notify.py")
        return "success"
    if result == "ambiguous":
        log("FEISHU_AMBIGUOUS: notify.py 结果不确定，不自动回退 Webhook")
        return "ambiguous"
    if result == "unavailable":
        log("NOTIFY: 青龙 notify.py 不可用（本地运行属正常），使用 Webhook")
    else:
        log("NOTIFY_FALLBACK: notify.py 明确失败，尝试 Webhook")
    webhook_result = _try_webhook(webhook_url, text, proxy_url)
    if webhook_result == "success":
        log("NOTIFY_OK: webhook")
        return "success"
    if webhook_result == "ambiguous":
        log("FEISHU_WEBHOOK_AMBIGUOUS: Webhook 结果不确定，不自动重试")
        return "ambiguous"
    if webhook_result == "unavailable":
        log("FEISHU_FAILED: 未配置 AGENTROUTER_WEBHOOK_URL")
        return "failed"
    log("FEISHU_FAILED: Webhook 明确失败")
    return "failed"


# ---------------------------------------------------------------------------
# 全局锁（单一全局锁；冲突时 TASK_NOT_EXECUTED + 退出码 3）
# ---------------------------------------------------------------------------

def acquire_lock(data_dir):
    lock_dir = os.path.join(data_dir, "locks")
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_dir, "global.lock")
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip()
        log("TASK_NOT_EXECUTED: 已有实例在运行，本次未执行任何账号、未发送通知")
        if holder:
            log(f"锁冲突原因: 持有者 {holder}")
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={utc_now()}\n")
    handle.flush()
    return handle


def release_lock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    handle.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(description="AgentRouter 青龙签到脚本（单文件）")
    parser.add_argument(
        "--mode",
        default="full",
        choices=["full", "single", "dry-run", "site-connectivity", "feishu-connectivity", "session-check"],
        help="执行模式，默认 full",
    )
    parser.add_argument("--account", default=None, help="single/session-check 模式下的账号 username")
    return parser.parse_args(argv)


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def main(argv=None):
    args = _parse_args(argv)
    data_dir = _env("AGENTROUTER_DATA_DIR", DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR
    base_url = _env("AGENTROUTER_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    proxy_url = _env("PROXY_URL")
    env_proxy = any(
        _env(name) for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    )
    webhook_url = _env("AGENTROUTER_WEBHOOK_URL")
    webhook_ok = (
        bool(webhook_url)
        and webhook_url.lower().startswith(("http://", "https://"))
        and not any(ch.isspace() for ch in webhook_url)
    )

    lock = acquire_lock(data_dir)
    if lock is None:
        return 3
    try:
        log(f"通知配置: Webhook "
            f"{'已设置' if webhook_ok else ('格式异常（应为纯 URL）' if webhook_url else '未设置')}")
        log(f"代理配置: PROXY_URL={'已设置' if proxy_url else '未设置'}；"
            f"环境代理(HTTP(S)_PROXY)={'已设置' if env_proxy else '未设置'}")
        if args.mode == "site-connectivity":
            client = HttpClient("site_connectivity", base_url, proxy_url, data_dir)
            resp = api_status(client)
            if resp.status == 200 and _ok(resp):
                log("SITE_OK: 可访问 /api/status")
                return 0
            log(f"SITE_FAILED: HTTP {resp.status}")
            return 2

        if args.mode == "feishu-connectivity":
            status = notify_summary("AgentRouter 飞书连通性测试", proxy_url, webhook_url)
            return 0 if status == "success" else 2

        if args.mode == "session-check":
            if not args.account or not args.account.strip():
                log("SESSION_CHECK_INVALID: 缺少或非法的 --account 参数（应为 username）")
                return 2
            accounts, errors = load_accounts()
            if errors:
                log("CONFIG_ERROR: 账号配置校验失败")
                for error in errors:
                    log(f"  - {error}")
                return 2
            target_key = args.account.strip().casefold()
            target = next((a for a in accounts if a["username"].casefold() == target_key), None)
            if target is None:
                log(f"SESSION_CHECK_INVALID: 账号 {args.account} 不在配置中")
                return 2
            client = HttpClient(target["username"], base_url, proxy_url, data_dir)
            cookie_exists = os.path.exists(client.cookie_path)
            cookie_count = len(list(client.jar))
            cookie_names = ",".join(sorted({c.name for c in client.jar})) or "无"
            log(f"SESSION_CHECK: 会话文件 {'存在' if cookie_exists else '不存在'}，"
                f"加载 Cookie 数量={cookie_count}，名称={cookie_names}")
            log(f"SESSION_CHECK: 已保存 user_id={'已知' if client.user_id is not None else '未知'}")
            try:
                with_header = api_self(client)
                without_header = client.get("/api/user/self", omit_user_header=True)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                log(f"SESSION_CHECK_FAILED: 网络错误 {type(exc).__name__}")
                return 2
            log(f"SESSION_CHECK: 带 New-API-User 头 HTTP {with_header.status} success={_ok(with_header)}")
            log(f"SESSION_CHECK: 不带 New-API-User 头 HTTP {without_header.status} success={_ok(without_header)}")
            if _ok(with_header) or _ok(without_header):
                log("SESSION_CHECK_OK: 已保存的 Cookie 会话仍然有效")
                return 0
            log("SESSION_CHECK_FAILED: 已保存的 Cookie 会话无效（站点未复用或 Cookie 未持久化）")
            return 2

        accounts, errors = load_accounts()
        if errors:
            log("CONFIG_ERROR: 账号配置校验失败")
            for error in errors:
                log(f"  - {error}")
            return 2

        enabled = [account for account in accounts if account["enabled"]]
        log(f"启用账号数量: {len(enabled)}")
        log("启用账号: " + (", ".join(a["username"] for a in enabled) if enabled else "无"))

        if args.mode == "dry-run":
            log("DRY_RUN_OK: 配置解析通过，未启动登录")
            return 0

        if not enabled:
            log("没有启用的账号，任务结束")
            return 0

        if args.mode == "single":
            if not args.account:
                log("SINGLE_TARGET_INVALID: 缺少 --account 参数")
                return 2
            target_key = args.account.strip().casefold()
            enabled = [a for a in enabled if a["username"].casefold() == target_key]
            if not enabled:
                log(f"SINGLE_TARGET_INVALID: 账号 {args.account} 不存在或未启用")
                return 2

        results = []
        started = time.monotonic()
        for index, account in enumerate(enabled):
            remaining = RUN_BUDGET_SECONDS - (time.monotonic() - started)
            if remaining < MIN_ACCOUNT_START_BUDGET_SECONDS:
                for rest in enabled[index:]:
                    results.append(_not_executed(rest["username"], "RUN_BUDGET_EXHAUSTED"))
                break
            log(f"开始处理账号 {account['username']}")
            result = process_account(account, base_url, proxy_url, data_dir)
            results.append(result)
            print_account_result(result)

        summary = build_summary(results, len(enabled), use_display=True)
        log("")
        log(summary)
        log("")
        notify_summary(summary, proxy_url, webhook_url)
        return _exit_code(results)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
