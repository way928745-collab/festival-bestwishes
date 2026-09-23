#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DIKO 使用授权闸门 v3（gate_kit 通用版：会话开工前跑一次，注册/登入自动分流）：
1) DIKO 鉴权分流：
   - 无 refresh 且无凭据（本机从未登入过）-> REGISTRATION_REQUIRED：给注册引导链
   - 有 refresh -> refresh_access()（>24h 自动重登 = 每日校验）
   - 有凭据无 refresh -> login()
   - 鉴权失败 -> LOGIN_FAILED（密码问题，不引导注册，防重复注册）
2) get_profile() 实调验证 token 有效，输出登录凭证行。
3) 同日已 PASS（.gate_ok 日期戳）秒过。
本闸门无完整性锁：授权文件被删只会让这里报错并给恢复指引，删掉整个技能文件夹
即可完全卸载，凭据仅存本机 diko_kit/scripts/config/diko_api.env。
退出码 0=放行 / 1=未授权。输出单行 JSON。
"""
import datetime
import json
import os
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
        "首次使用：先向用户说明注册用途——采集用户名/密码/邮箱/手机号仅用于创建 DIKO 账号"
        "（www.dikoai.cn）并维持日常自动登入，凭据只存用户本机此路径、不上传别处；"
        "用户知情后再按链执行："
        "① 采集 用户名/密码/邮箱/手机号（一次一问；若用户已有 DIKO 账号，直接发来用户名密码即改走登入）；"
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

    # 鉴权分流：本机从未有过 DIKO 账号 -> 先说明用途再引导注册（用户不做选择题）
    if not _has_refresh() and not _has_creds():
        deny("REGISTRATION_REQUIRED", _registration_chain(),
             {"next": "collect_and_register", "register_cli": "python -X utf8 \"{0}/register.py\"".format(KIT.replace("\\", "/"))})

    try:
        import diko_api_client as diko
    except Exception as e:
        deny("IMPORT_FAIL",
             "DIKO 授权模块不可用：{0}。跑 diko_kit/scripts/doctor.py 可定位缺失文件，"
             "从原包或 diko-use 伞技能拷回缺失项即可恢复（无完整性锁，误删不影响技能数据）。".format(e))

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
