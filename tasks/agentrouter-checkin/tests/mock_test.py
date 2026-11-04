#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentRouter-Checkin Mock 测试。

全部使用本地伪造的 HTTP 客户端与通知函数，不访问真实网站、不使用真实账号。
运行：python3 -I tests/mock_test.py
"""

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agentrouter_checkin", BASE / "agentrouter_checkin.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

ORIG_HTTP = mod.HttpClient
ORIG_QN = mod._try_qinglong_notify
ORIG_WH = mod._try_webhook

ENV_KEYS = (
    "AGENTROUTER_ACCOUNTS",
    "AGENTROUTER_DATA_DIR",
    "AGENTROUTER_BASE_URL",
    "PROXY_URL",
    "AGENTROUTER_WEBHOOK_URL",
)


class FakeHttpClient:
    responder = None

    def __init__(self, account_id, base_url, proxy_url, data_dir):
        self.account_id = account_id
        self.user_id = None

    def get(self, path, omit_user_header=False):
        return FakeHttpClient.responder(self.account_id, "GET", path, None)

    def post_json(self, path, payload):
        return FakeHttpClient.responder(self.account_id, "POST", path, payload)

    def save_session(self):
        pass


def ok(data, message=""):
    return mod.ApiResponse(200, {"success": True, "message": message, "data": data}, "")


def fail(status, message=""):
    return mod.ApiResponse(status, {"success": False, "message": message}, message)


def success_responder(username, uid=1, checked=True, before=1_000_000, after=1_025_000, session_valid=False):
    """标准账号应答机：覆盖 无会话 / 有效旧会话 两种路径。"""
    state = {"phase": "probe"}

    def respond(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": False, "quota_per_unit": 500000})
        if path == "/api/user/self":
            if state["phase"] == "probe":
                state["phase"] = "probe_done"
                if session_valid:
                    return ok({"username": username, "id": uid, "quota": before, "used_quota": 0})
                return fail(401, "无权进行此操作，未登录且未提供 access token")
            if state["phase"] == "after_logout_check":
                state["phase"] = "logged_out_ok"
                return fail(401, "无权进行此操作，未登录且未提供 access token")
            if state["phase"] == "after_login":
                return ok({"username": username, "id": uid, "quota": after, "used_quota": 5})
            return fail(401, "未登录")
        if path == "/api/user/logout":
            state["phase"] = "after_logout_check"
            return ok(None)
        if path.startswith("/api/user/login"):
            state["phase"] = "after_login"
            return ok({"username": username, "id": uid, "checked_in": checked})
        return fail(404, "not found")

    return respond


def dispatch(responders):
    def respond(account_id, method, path, payload):
        return responders[account_id](account_id, method, path, payload)
    return respond


def run_case(name, accounts, responder, expect_code, expect_in=(), expect_not_in=(),
             notify=None, lock_holder=False):
    tmp = tempfile.mkdtemp(prefix="ar-test-")
    backup = {key: os.environ.get(key) for key in ENV_KEYS}
    os.environ["AGENTROUTER_ACCOUNTS"] = accounts if isinstance(accounts, str) else json.dumps(accounts)
    os.environ["AGENTROUTER_DATA_DIR"] = tmp
    os.environ["AGENTROUTER_BASE_URL"] = "https://example.invalid"
    os.environ["PROXY_URL"] = ""
    os.environ["AGENTROUTER_WEBHOOK_URL"] = ""
    FakeHttpClient.responder = responder
    mod.HttpClient = FakeHttpClient
    captured = {"notify_text": None}
    if notify is None:
        def _fake_qn(title, text):
            captured["notify_text"] = text
            return "success"
        mod._try_qinglong_notify = _fake_qn
        mod._try_webhook = lambda *a: "success"
    else:
        mod._try_qinglong_notify, mod._try_webhook = notify

    holder = None
    if lock_holder:
        lock_dir = os.path.join(tmp, "locks")
        os.makedirs(lock_dir, exist_ok=True)
        holder = open(os.path.join(lock_dir, "global.lock"), "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        holder.write("pid=99999 started=test\n")
        holder.flush()

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = mod.main(["--mode", "full"])
    finally:
        if holder is not None:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        mod.HttpClient = ORIG_HTTP
        mod._try_qinglong_notify = ORIG_QN
        mod._try_webhook = ORIG_WH
        for key, value in backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    output = buffer.getvalue()
    problems = []
    if code != expect_code:
        problems.append(f"退出码 {code} != 期望 {expect_code}")
    for text in expect_in:
        if text not in output:
            problems.append(f"输出缺少: {text!r}")
    for text in expect_not_in:
        if text in output:
            problems.append(f"输出不应包含: {text!r}")
    return problems, output, captured


CASES = []


def case(name):
    def decorator(func):
        CASES.append((name, func))
        return func
    return decorator


@case("1. 单账号成功（无旧会话）")
def test_single_success():
    accounts = [{"username": "user_secret_1", "password": "P@ss_SECRET_123", "enabled": True}]
    problems, _, captured = run_case(
        "single", accounts, success_responder("user_secret_1"),
        expect_code=0,
        expect_in=["完成: 1", "签到前余额: 未知", "签到后余额: $2.05（1025000 quota）",
                   "总余额: $2.05（1025000 quota）"],
        expect_not_in=["P@ss_SECRET_123"],
    )
    notify_text = captured["notify_text"] or ""
    if "【user_secret_1】" not in notify_text:
        problems.append("推送文本应使用 username 作为标题")
    return problems


@case("2. 多账号成功")
def test_multi_success():
    accounts = [
        {"username": "ua", "password": "pa", "enabled": True},
        {"username": "ub", "password": "pb", "enabled": True},
    ]
    responder = dispatch({
        "ua": success_responder("ua", uid=1, after=1_025_000),
        "ub": success_responder("ub", uid=2, after=525_000),
    })
    problems, output, captured = run_case(
        "multi", accounts, responder,
        expect_code=0,
        expect_in=["【ua】", "【ub】", "完成: 2", "总余额: $3.10（1550000 quota）"],
    )
    notify_text = captured["notify_text"] or ""
    if "【ua】" not in notify_text or "【ub】" not in notify_text:
        problems.append("推送文本应使用 username 作为标题")
    return problems


@case("3. 一个账号失败，其他继续")
def test_partial_failure():
    accounts = [
        {"username": "ua", "password": "pa", "enabled": True},
        {"username": "ub", "password": "pb", "enabled": True},
        {"username": "uc", "password": "pc", "enabled": True},
    ]

    def failing_login(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": False, "quota_per_unit": 500000})
        if path == "/api/user/self":
            return fail(401, "未登录")
        if path.startswith("/api/user/login"):
            return fail(200, "用户名或密码错误")
        return fail(404)

    responder = dispatch({
        "ua": success_responder("ua", uid=1),
        "ub": failing_login,
        "uc": success_responder("uc", uid=3),
    })
    problems, _, _ = run_case(
        "partial", accounts, responder,
        expect_code=1,
        expect_in=["完成: 2", "失败: 1", "LOGIN_FAILED", "【ub】"],
    )
    return problems


@case("4. 账号配置错误（非法 JSON / 重复 username / 空密码）")
def test_config_errors():
    problems = []
    p, _, _ = run_case("bad-json", "{not-json", lambda *a: fail(500), expect_code=2,
                    expect_in=["CONFIG_ERROR", "ACCOUNTS_JSON_INVALID"])
    problems += p
    duplicate = [
        {"username": "ua", "password": "pa", "enabled": True},
        {"username": "ua", "password": "pb", "enabled": True},
    ]
    p, _, _ = run_case("dup-username", duplicate, lambda *a: fail(500), expect_code=2,
                    expect_in=["CONFIG_ERROR", "username 重复"])
    problems += p
    empty_pwd = [{"username": "ua", "password": "", "enabled": True}]
    p, _, _ = run_case("empty-pwd", empty_pwd, lambda *a: fail(500), expect_code=2,
                    expect_in=["CONFIG_ERROR", "password 为空"])
    problems += p
    return problems


@case("5. 余额查询失败（登录后用户信息失败）")
def test_balance_failure():
    state = {"logged_in": False}

    def responder(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": False, "quota_per_unit": 500000})
        if path == "/api/user/self":
            if state["logged_in"]:
                return fail(500, "server error")
            return fail(401, "未登录")
        if path.startswith("/api/user/login"):
            state["logged_in"] = True
            return ok({"username": "ua", "id": 1, "checked_in": True})
        return fail(404)

    accounts = [{"username": "ua", "password": "pa", "enabled": True}]
    problems, _, _ = run_case(
        "balance-fail", accounts, responder,
        expect_code=2,
        expect_in=["BALANCE_FAILED", "签到后余额: 未知", "总余额: 无法计算"],
    )
    return problems


@case("6. 配置 expected_name 时严格比对：不一致则失败")
def test_identity_mismatch():
    state = {"phase": "probe"}

    def responder(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": False, "quota_per_unit": 500000})
        if path == "/api/user/self":
            if state["phase"] == "probe":
                return fail(401, "未登录")
            return ok({"username": "intruder", "id": 99, "quota": 1_020_000, "used_quota": 0})
        if path.startswith("/api/user/login"):
            state["phase"] = "after_login"
            return ok({"username": "intruder", "id": 99, "checked_in": True})
        return fail(404)

    accounts = [{"username": "ua", "password": "pa",
                 "enabled": True, "expected_name": "ua"}]
    problems, _, _ = run_case(
        "identity", accounts, responder,
        expect_code=2,
        expect_in=["IDENTITY_MISMATCH", "签到后余额: 未知"],
        expect_not_in=["1020 quota"],
    )
    return problems


@case("6b. 未配置 expected_name 时，名称不同仍按登录成功")
def test_identity_relaxed():
    state = {"phase": "probe"}

    def responder(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": False, "quota_per_unit": 500000})
        if path == "/api/user/self":
            if state["phase"] == "probe":
                return fail(401, "未登录")
            return ok({"username": "someone_else", "id": 99, "quota": 1_020_000, "used_quota": 0})
        if path.startswith("/api/user/login"):
            state["phase"] = "after_login"
            return ok({"username": "someone_else", "id": 99, "checked_in": True})
        return fail(404)

    accounts = [{"username": "ua", "password": "pa", "enabled": True}]
    problems, _, _ = run_case(
        "identity-relaxed", accounts, responder,
        expect_code=0,
        expect_in=["完成: 1", "签到后余额: $2.04（1020000 quota）", "注意: 登录返回账号与配置不一致"],
    )
    return problems


@case("7. 通知失败不影响签到结果")
def test_notification_failure():
    accounts = [{"username": "ua", "password": "pa", "enabled": True}]
    problems, _, _ = run_case(
        "notify-fail", accounts, success_responder("ua"),
        expect_code=0,
        expect_in=["完成: 1", "FEISHU_FAILED"],
        notify=(lambda *a: "definite_failure", lambda *a: "definite_failure"),
    )
    # 结果不确定：不自动回退 Webhook
    called = {"webhook": False}

    def webhook_should_not_run(*args):
        called["webhook"] = True
        return "success"

    p, _, _ = run_case(
        "notify-ambiguous", accounts, success_responder("ua"),
        expect_code=0,
        expect_in=["FEISHU_AMBIGUOUS"],
        notify=(lambda *a: "ambiguous", webhook_should_not_run),
    )
    problems += p
    if called["webhook"]:
        problems.append("notify.py 结果不确定时不应调用 Webhook")
    return problems


@case("8. 出现验证码/Turnstile 时暂停账号")
def test_human_verification():
    def responder(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": True, "quota_per_unit": 500000})
        if path == "/api/user/self":
            return fail(401, "未登录")
        if path.startswith("/api/user/login"):
            raise AssertionError("启用 Turnstile 时不应尝试登录")
        return fail(404)

    accounts = [{"username": "ua", "password": "pa", "enabled": True}]
    problems, _, _ = run_case(
        "captcha", accounts, responder,
        expect_code=2,
        expect_in=["HUMAN_ACTION_REQUIRED", "需人工: 1"],
    )
    return problems


@case("附加A. 有效旧会话：前余额→退出→登录→后余额")
def test_session_valid():
    accounts = [{"username": "ua", "password": "pa", "enabled": True}]
    problems, _, _ = run_case(
        "session-valid", accounts,
        success_responder("ua", uid=1, before=1_000_000, after=1_025_000, session_valid=True),
        expect_code=0,
        expect_in=["签到前余额: $2.00（1000000 quota）", "签到后余额: $2.05（1025000 quota）",
                   "余额变化: +$0.05（+25000 quota）"],
    )
    return problems


@case("附加C. 配置为邮箱、返回 username 不同但 email 匹配 → 成功")
def test_email_identity():
    state = {"phase": "probe"}

    def responder(account_id, method, path, payload):
        if path == "/api/status":
            return ok({"turnstile_check": False, "quota_per_unit": 500000})
        if path == "/api/user/self":
            if state["phase"] == "probe":
                state["phase"] = "probe_done"
                return fail(401, "未登录")
            return ok({"username": "site_name", "email": "user@example.com", "id": 7, "quota": 520_000, "used_quota": 1})
        if path.startswith("/api/user/login"):
            state["phase"] = "after_login"
            return ok({"username": "site_name", "email": "user@example.com", "id": 7, "checked_in": True})
        return fail(404)

    accounts = [{"username": "user@example.com", "password": "pa",
                 "enabled": True, "expected_name": "user@example.com"}]
    problems, _, _ = run_case(
        "email-identity", accounts, responder,
        expect_code=0,
        expect_in=["完成: 1", "签到后余额: $1.04（520000 quota）"],
    )
    return problems


@case("附加B. 锁冲突：TASK_NOT_EXECUTED + 退出码 3")
def test_lock_conflict():
    accounts = [{"username": "ua", "password": "pa", "enabled": True}]
    problems, _, _ = run_case(
        "lock", accounts, lambda *a: fail(500),
        expect_code=3,
        expect_in=["TASK_NOT_EXECUTED", "锁冲突原因"],
        lock_holder=True,
    )
    return problems


def main():
    total = 0
    failed = 0
    for name, func in CASES:
        total += 1
        try:
            problems = func()
        except Exception:
            problems = ["测试执行异常:\n" + traceback.format_exc()]
        if problems:
            failed += 1
            print(f"FAIL  {name}")
            for problem in problems:
                print(f"      - {problem}")
        else:
            print(f"PASS  {name}")
    print(f"\n结果: {total - failed}/{total} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
