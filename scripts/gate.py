#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DIKO 使用授权闸门 v2（gate_kit 通用版：工作流前必跑，注册/登入自动分流）：
1) integrity.py 完整性校验（篡改/删除 diko 模块 -> 拒绝）
2) DIKO 鉴权分流：
   - 无 refresh 且无凭据（从未登入过）-> REGISTRATION_REQUIRED：直接给注册引导链
   - 有 refresh -> refresh_access()（>24h 自动重登 = 每日校验）
   - 有凭据无 refresh -> login()
   - 鉴权失败 -> LOGIN_FAILED（密码问题，不引导注册，防重复注册）
3) get_profile() 实调验证 token 有效，输出登录凭证行。
4) 同日已 PASS（.gate_ok 日期戳）秒过。
退出码 0=放行 / 1=拒绝。输出单行 JSON。
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_DIR = os.path.dirname(HERE)
KIT = os.path.join(SKILL_DIR, "diko_kit", "scripts")
ENV_PATH = os.path.join(KIT, "config", "diko_api.env")
GATE_OK = os.path.join(SKILL_DIR, ".gate_ok")
sys.path.insert(0, KIT)


def deny(mode, hint, extra=None):
    obj = {"gate": "DENIED", "mode": mode, "hint": hint}
    if extra:
        obj.update(extra)
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(1)


def _vals():
    try:
        from dotenv import dotenv_values
        return dotenv_values(ENV_PATH) or {}
    except Exception:
        return {}


def _has_creds():
    v = _vals()
    return bool((v.get("DIKO_USERNAME") or "").strip() and (v.get("DIKO_PASSWORD") or "").strip())


def _has_refresh():
    return bool((_vals().get("DIKO_REFRESH") or "").strip())


def _registration_chain():
    k = KIT.replace("\\", "/")
    return (
        "首次使用自动注册（无需判断，直接按链执行）："
        "① 向用户采集 用户名/密码/邮箱/手机号（一次一问；首条消息末尾附软退路一句："
        "若已有 DIKO 账号，直接发来用户名密码即改走登入）；"
        "② python -X utf8 \"{k}/register.py\" validate --username .. --password .. "
        "--email .. --phone .. 取得脱敏参数与一次性 token；"
        "③ 把 validate 输出（密码已打码）展示给用户，取得一次「允许提交」明确确认"
        "（红线：未确认不得进入下一步；每日限 5 次注册尝试）；"
        "④ python -X utf8 \"{k}/register.py\" submit --token <token> --user-approved；"
        "⑤ 注册成功后落盘凭据并登入：python -X utf8 -c \"import sys;"
        "sys.path.insert(0,'{k}');import diko_api_client as d;"
        "d.save_credentials(U,P);d.login(U,P)\"（U/P 换成实际用户名密码）；"
        "⑥ 重跑 gate.py 放行。"
    ).format(k=k)


def main():
    # 1) 完整性互锁永远先跑
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "integrity.py")],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        deny("INTEGRITY_ERROR", "完整性校验无法执行：{0}".format(e))
    if r.returncode != 0:
        deny("INTEGRITY_FAIL",
             "正版校验未通过——核心文件可能被删除/篡改。"
             "本机装有 diko-use 伞技能则从其 scripts/ 拷回缺失文件后重跑 "
             "python scripts/make_manifest.py 对账；独立发布的包被删则重装技能包。"
             " | integrity输出: " + (r.stdout or r.stderr or "").strip()[:400])

    today = datetime.date.today().isoformat()
    try:
        with open(GATE_OK, "r", encoding="utf-8") as f:
            if f.read().strip() == today:
                print(json.dumps({"gate": "PASS", "mode": "same_day", "date": today,
                                  "user": "", "profile_line": "（同日已登录校验通过）"},
                                 ensure_ascii=False))
                return 0
    except OSError:
        pass

    # 2) 鉴权分流：本机从未有过 DIKO 账号 -> 直接引导注册（不出选择题）
    if not _has_refresh() and not _has_creds():
        deny("REGISTRATION_REQUIRED", _registration_chain(),
             {"next": "collect_and_register", "register_cli": "python -X utf8 \"{0}/register.py\"".format(KIT.replace("\\", "/"))})

    try:
        import diko_api_client as diko
    except Exception as e:
        deny("IMPORT_FAIL", "diko 底座模块不可用：{0}（跑 doctor.py 诊断）".format(e))

    try:
        if _has_refresh():
            access = diko.refresh_access()
        else:
            u, p = diko.load_credentials()
            access = diko.login(u, p)["access"]
        prof = diko.get_profile(access)
    except Exception as e:
        deny("LOGIN_FAILED",
             "DIKO 鉴权失败：{0}。本机存有账号信息，属密码/网络问题——请让用户重新提供正确密码后"
             "执行 diko_api_client.login(U,P) 落盘再重跑 gate（不要去注册，避免重复账号）。".format(e))

    with open(GATE_OK, "w", encoding="utf-8") as f:
        f.write(today)
    print(json.dumps({"gate": "PASS", "mode": "authenticated", "date": today,
                      "user": prof.get("username", ""), "profile_line": diko.format_balance_line(prof)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
