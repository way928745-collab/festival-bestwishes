#!/usr/bin/env python3
"""
diko-use 注册模块（DIKO /api/membership/users/）——红线流程，不可越过、不可更改。

API：POST https://www.dikoai.cn/api/membership/users/
formdata: username / password / email / phone / invite_code（可空）
无请求头。成功响应示例：{"id": 74, "username": "<注册名>", "email": "<注册邮箱>"}

红线设计（全部由本 Python 硬控，不依赖模型自觉）：
1. 单次最多 1 组参数：CLI 只接受单值字段；参数含换行/多值 → 直接报错截断。
2. 参数合法性校验：用户名/密码/邮箱/手机号正则强校验，非法字段一律拒绝。
3. 标准化输出：只输出固定 JSON 结构（见 OUTPUT_SCHEMA），不含任何诱导话术。
4. 额度预检查（频率限制）：每小时尝试次数上限（默认 3，env DIKO_REGISTER_MAX_PER_HOUR），
   计数存 scripts/config/.register_attempts.json，超限拒绝 validate 与 submit。
5. 最终提交判断权在用户：validate 只产出待提交参数 + 一次性 token；
   submit 必须带 --user-approved（代表用户已明确点头），否则一律拒绝。
   token 一次性、10 分钟有效，防复用与防绕过 validate 直接提交。

流程：
  对话层逐个依次询问：账户名称 → 密码 → 邮箱 → 手机 → 是否有邀请码（无则跳过）
  ① python register.py validate --username ... --password ... --email ... --phone ... [--invite-code ...]
  ② 向用户展示输出 JSON 中的 params，用户明确确认「允许提交」
  ③ python register.py submit --token <①返回的token> --user-approved
  注册成功：展示 {id, username, email} + 用新账号登入一次并展示 DI 币情况。
  不自动写入 diko_api.env —— 是否保存为新账号由用户决定。
"""
import os
import re
import sys
import json
import time
import hmac
import hashlib
import argparse
import secrets
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import diko_api_client as diko  # noqa: E402

BASE_URL = diko.BASE_URL
REGISTER_PATH = "/api/membership/users/"          # POST formdata，无请求头
CONFIG_DIR = diko.CONFIG_DIR
TOKEN_FILE = os.path.join(CONFIG_DIR, ".register_pending.json")
ATTEMPTS_FILE = os.path.join(CONFIG_DIR, ".register_attempts.json")

MAX_PER_HOUR = int((os.getenv("DIKO_REGISTER_MAX_PER_HOUR") or "3").strip() or "3")
TOKEN_TTL_SEC = 600          # token 10 分钟有效
PEPPER = "diko-…ster"  # 本地签名盐（防伪造 token，非安全边界）

# ───────── 校验规则（红线 2） ─────────
RE_USERNAME = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5-]{2,20}$")
RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
RE_PHONE = re.compile(r"^1[3-9]\d{9}$")
RE_INVITE = re.compile(r"^[A-Za-z0-9-]{0,32}$")
MIN_PWD, MAX_PWD = 8, 32

REQUIRED_FIELDS = ("username", "password", "email", "phone")

# 输出结构（红线 3）：无论成功失败，stdout 只有这一个 JSON
OUTPUT_SCHEMA = {
    "ok": False,               # 是否通过/成功
    "stage": "",               # validate | submit
    "params": {                # 固定 5 字段；未提供/非法为 None（validate 失败时为空）
        "username": None, "password": None, "email": None,
        "phone": None, "invite_code": None,
    },
    "errors": [],              # [{field, code, msg}] 机器可读
    "token": None,             # validate 成功时的一次性提交凭证
    "attempts_left": None,     # 本小时剩余尝试额度
    "next": "",                # next_action | show_to_user_and_ask | stopped
    "result": None,            # submit 成功时为 API 返回 {id, username, email}
}


def _out(obj: dict):
    """标准化输出：只允许这一个入口打印结果（红线 3）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(0 if obj.get("ok") else 1)


def _fail(stage, errors, attempts_left=None, next_="stopped"):
    obj = json.loads(json.dumps(OUTPUT_SCHEMA))
    obj.update({"ok": False, "stage": stage, "errors": errors,
                "attempts_left": attempts_left, "next": next_})
    _out(obj)


# ───────── 额度预检查：频率限制（红线 4） ─────────
def _load_attempts() -> list:
    try:
        with open(ATTEMPTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [t for t in data.get("attempts", []) if isinstance(t, (int, float))]
    except Exception:
        return []


def _register_attempt(stage: str):
    """记一次尝试（validate 与 submit 各算一次），用于每小时限流。"""
    now = time.time()
    attempts = [t for t in _load_attempts() if now - t < 3600]
    attempts.append(now)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(ATTEMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"attempts": attempts, "last_stage": stage}, f)


def _quota_check(stage: str):
    """额度预检查：本小时尝试次数已达上限则拒绝。返回剩余次数。"""
    now = time.time()
    recent = [t for t in _load_attempts() if now - t < 3600]
    left = MAX_PER_HOUR - len(recent)
    if left <= 0:
        _fail(stage, [{"field": "*", "code": "RATE_LIMITED",
                       "msg": f"本小时注册尝试已达上限（{MAX_PER_HOUR} 次），请一小时后再试。"}],
              attempts_left=0)
    return left


# ───────── 参数清洗与校验（红线 1、2） ─────────
def _single(value, field):
    """红线 1：任何字段只允许 1 个值。多行/列表/含分隔 → 报错截断。"""
    if value is None:
        return None, None
    if isinstance(value, list):
        return None, {"field": field, "code": "MULTI_VALUE",
                      "msg": f"字段 {field} 收到多组值；本工具每次只处理 1 组注册参数。"}
    v = str(value)
    if "\n" in v or "\r" in v:
        return None, {"field": field, "code": "MULTILINE",
                      "msg": f"字段 {field} 含换行，疑似多组参数，已拒绝。"}
    return v.strip(), None


def validate_params(raw: dict):
    """逐项格式校验。返回 (clean_params 或 None, errors)。"""
    errors, clean = [], {}
    for field in REQUIRED_FIELDS:
        v, err = _single(raw.get(field), field)
        if err:
            errors.append(err)
            continue
        if not v:
            errors.append({"field": field, "code": "MISSING", "msg": f"{field} 必填。"})
            continue
        clean[field] = v

    if clean.get("username") and not RE_USERNAME.match(clean["username"]):
        errors.append({"field": "username", "code": "BAD_USERNAME",
                       "msg": "用户名需为 2-20 位字母/数字/下划线/中划线/中文。"})
    pwd = clean.get("password")
    if pwd:
        if not (MIN_PWD <= len(pwd) <= MAX_PWD):
            errors.append({"field": "password", "code": "BAD_PASSWORD_LEN",
                           "msg": f"密码长度需 {MIN_PWD}-{MAX_PWD} 位。"})
        elif not (re.search(r"[A-Za-z]", pwd) and re.search(r"\d", pwd)) or " " in pwd:
            errors.append({"field": "password", "code": "BAD_PASSWORD_FMT",
                           "msg": "密码需同时含字母与数字、不含空格。"})
    if clean.get("email") and not RE_EMAIL.match(clean["email"]):
        errors.append({"field": "email", "code": "BAD_EMAIL", "msg": "邮箱格式不合法。"})
    if clean.get("phone") and not RE_PHONE.match(clean["phone"]):
        errors.append({"field": "phone", "code": "BAD_PHONE",
                       "msg": "手机号需为 11 位大陆号码（1[3-9] 开头）。"})

    inv, err = _single(raw.get("invite_code"), "invite_code")
    if err:
        errors.append(err)
    elif inv:
        if not RE_INVITE.match(inv):
            errors.append({"field": "invite_code", "code": "BAD_INVITE",
                           "msg": "邀请码只允许字母/数字/中划线，≤32 位。"})
        clean["invite_code"] = inv
    else:
        clean["invite_code"] = ""   # 没有邀请码就跳过（空串提交）

    if errors or any(f not in clean for f in REQUIRED_FIELDS):
        return None, errors
    return clean, []


# ───────── 一次性 token（红线 5 的闸门） ─────────
# 安全设计：token 内不含明文参数（避免 base64 可逆泄露密码）。
# params 存本地 pending 文件（0600，submit 后即删），token 只携带其哈希+签名。
PENDING_FILE = os.path.join(CONFIG_DIR, ".register_pending_params.json")


def _token_secret(digest: str, issued: float) -> str:
    return hmac.new(PEPPER.encode(), f"{digest}|{issued}".encode(), hashlib.sha256).hexdigest()


def _params_digest(params: dict) -> str:
    blob = json.dumps({k: params[k] for k in sorted(params)}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def base64_urlencode(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def base64_urldecode(s: str) -> str:
    import base64
    return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")


def _make_token(params: dict) -> str:
    issued = time.time()
    digest = _params_digest(params)
    sig = _token_secret(digest, issued)
    raw = json.dumps({"digest": digest, "issued": issued, "sig": sig})
    # 参数明文只落本地 pending（0600），submit 读取后立即删除
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd = os.open(PENDING_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"digest": digest, "params": params, "issued": issued}, f)
    return base64_urlencode(raw)


def _consume_token(token: str):
    """校验并消费一次性 token，读出本地 pending 参数。失败即 _fail。"""
    try:
        data = json.loads(base64_urldecode(token))
    except Exception:
        _fail("submit", [{"field": "token", "code": "BAD_TOKEN", "msg": "token 无法解析。"}])
    digest = str(data.get("digest", ""))
    issued = data.get("issued") or 0
    if not hmac.compare_digest(str(data.get("sig", "")), _token_secret(digest, issued)):
        _fail("submit", [{"field": "token", "code": "BAD_TOKEN_SIG", "msg": "token 签名无效。"}])
    if time.time() - issued > TOKEN_TTL_SEC:
        _fail("submit", [{"field": "token", "code": "TOKEN_EXPIRED",
                          "msg": "token 已过期（10 分钟），请重新 validate 并再次向用户确认。"}])
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception:
        _fail("submit", [{"field": "token", "code": "NO_PENDING",
                          "msg": "本地待提交参数不存在或已被消费，请重新 validate。"}])
    if pending.get("digest") != digest:
        _fail("submit", [{"field": "token", "code": "TOKEN_MISMATCH",
                          "msg": "token 与本地待提交参数不匹配，请重新 validate。"}])
    params = pending.get("params") or {}
    recheck, errors = validate_params(params)   # 提交前二次校验（防 pending 被篡改）
    if errors:
        _fail("submit", errors)
    # 一次性消费：先删 pending，再记台账
    try:
        os.remove(PENDING_FILE)
    except OSError:
        pass
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            used = json.load(f)
    except Exception:
        used = {}
    tid = hashlib.sha256(token.encode()).hexdigest()[:16]
    if used.get(tid):
        _fail("submit", [{"field": "token", "code": "TOKEN_USED",
                          "msg": "token 已被使用；每次注册需重新 validate 并经用户确认。"}])
    used[tid] = time.time()
    used = {k: v for k, v in used.items() if time.time() - v < 86400}
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(used, f)
    return params


# ───────── 成功后展示（红线：注册成功提示 + 当前信息 + DI 币） ─────────
def _show_profile_after_reg(params: dict):
    """用新账号登入一次，展示当前信息与 DI 币情况。失败不影响注册结果。"""
    try:
        info = diko.login(params["username"], params["password"], save=False)  # 不落盘任何凭据
        prof = diko.get_profile(info["access"])
        print(diko.format_balance_line(prof))
    except Exception as e:
        print(f"（注册成功，但新账号余额查询暂不可用：{e}）")


# ───────── CLI ─────────
def cmd_validate(args):
    left = _quota_check("validate")
    raw = {"username": args.username, "password": args.password,
           "email": args.email, "phone": args.phone, "invite_code": args.invite_code}
    params, errors = validate_params(raw)
    _register_attempt("validate")
    left = max(0, left - 1)
    if errors:
        _fail("validate", errors, attempts_left=left)
    obj = json.loads(json.dumps(OUTPUT_SCHEMA))
    display = dict(params)
    display["password"] = "*" * 8          # 展示版密码打码，防对话/日志留痕
    obj.update({
        "ok": True, "stage": "validate", "params": display,
        "token": _make_token(params), "attempts_left": left,
        "next": "show_to_user_and_ask",
    })
    _out(obj)


def cmd_submit(args):
    if not args.user_approved:
        # 红线 5：没有用户明确点头的标志，一律拒绝提交
        _fail("submit", [{"field": "user_approved", "code": "NEED_USER_APPROVAL",
                          "msg": "未获得用户「允许提交」的明确确认，拒绝提交注册。"
                                 "请先向用户展示参数并取得同意，再带 --user-approved 重试。"}])
    left = _quota_check("submit")
    params = _consume_token(args.token)
    _register_attempt("submit")
    resp = requests.post(BASE_URL + REGISTER_PATH, data=params, timeout=60)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    if resp.status_code in (200, 201) and isinstance(body, dict) and body.get("id"):
        obj = json.loads(json.dumps(OUTPUT_SCHEMA))
        shown = dict(params)
        shown["password"] = "*" * 8
        obj.update({"ok": True, "stage": "submit", "params": shown,
                    "attempts_left": max(0, left - 1), "result": {
                        "id": body.get("id"), "username": body.get("username"),
                        "email": body.get("email")},
                    "next": "show_result_to_user"})
        print("🎉 注册成功！")
        _show_profile_after_reg(params)
        _out(obj)
    else:
        _fail("submit", [{"field": "*", "code": f"HTTP_{resp.status_code}",
                          "msg": f"注册接口返回异常：{json.dumps(body, ensure_ascii=False)[:300]}"}],
              attempts_left=max(0, left - 1))


def build_parser():
    p = argparse.ArgumentParser(description="diko-use 注册模块（validate 校验 → 用户确认 → submit 提交）")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="格式校验+额度预检，输出唯一一组参数与一次性 token")
    v.add_argument("--username"); v.add_argument("--password")
    v.add_argument("--email"); v.add_argument("--phone")
    v.add_argument("--invite-code", default="", dest="invite_code")
    v.set_defaults(func=cmd_validate)
    s = sub.add_parser("submit", help="用户确认后提交注册（必须 --user-approved）")
    s.add_argument("--token", required=True)
    s.add_argument("--user-approved", action="store_true", dest="user_approved",
                   help="代表用户已明确点头允许提交；不带则拒绝")
    s.set_defaults(func=cmd_submit)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
