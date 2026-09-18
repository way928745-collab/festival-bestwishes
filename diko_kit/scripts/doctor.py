#!/usr/bin/env python3
"""
diko-use 自检维修模块（doctor）——通用框架版。

三种用法：
    python doctor.py --self                       # 列出本技能具备哪些 diko 模块
    python doctor.py audit  "<目标技能目录>"       # 检查目标技能缺哪个 diko 模块（JSON 报告）
    python doctor.py repair "<目标技能目录>" --module register   # 补缺失模块（拷入 <目标>/diko_kit/）
    python doctor.py repair "<目标技能目录>" --module all [--force]

特征识别（在目标目录全部 .py/.md/.txt 中找签名）：
    login    = "jwt/create"          登入
    profile  = "membership/profile"  用户信息/DI币
    image    = "z_maxgen"            生图
    voice    = "z_model" + app 41    声音素材
    register = "membership/users"    注册

repair 规则（红线）：
- 只拷贝、不覆盖：目标已有同名文件时跳过（--force 才覆盖，且先备份 .bak）
- 拷入的是本技能的框架脚本（不含任何个人凭据）+ 空 env 模板
- 依赖模块自动附带：image/voice 依赖 login+profile → 一并拷 diko_api_client.py
- 修完必须再次 audit 复验
"""
import os
import re
import sys
import json
import shutil
import argparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))

# 模块登记表：签名 → 说明 + 需要拷贝的文件
MODULES = {
    "login": {
        "desc": "登入（access/refresh 自动续期）",
        "signatures": [r"jwt/create"],
        "files": ["diko_api_client.py"],
        "depends": [],
    },
    "profile": {
        "desc": "用户信息 / DI 币余额展示",
        "signatures": [r"membership/profile"],
        "files": ["diko_api_client.py"],
        "depends": ["login"],
    },
    "image": {
        "desc": "生图（文生图/图生图，/api/z_maxgen/）",
        "signatures": [r"z_maxgen"],
        "files": ["diko_image.py", "diko_api_client.py"],
        "depends": ["login", "profile"],
    },
    "voice": {
        "desc": "声音素材生成（z_model app_id=41）",
        "signatures": [r"z_model", r"(VOICE_APP_ID|app_id[\"'\s:=]{1,6}\d?\d?41)"],
        "files": ["diko_voice.py", "diko_api_client.py"],
        "depends": ["login", "profile"],
    },
    "register": {
        "desc": "注册（validate→用户确认→submit 两段式）",
        "signatures": [r"membership/users"],
        "files": ["register.py", "diko_api_client.py"],
        "depends": ["login"],
    },
}

ENV_TEMPLATE = """# DIKO 凭据模板（发布版全空 · 不得含任何个人信息）
# 首次使用交互式写入；登入成功后自动写入 REFRESH 字段。无账号先注册：www.dikoai.cn
DIKO_BASE_URL=https://www.dikoai.cn
DIKO_USERNAME=
DIKO_PASSWORD=
DIKO_REFRE...ME=
DIKO_IMAGE_MODEL=无限生图3.1flash
DIKO_IMAGE_RESOLUTION=2K
DIKO_REGISTER_MAX_PER_HOUR=3
"""

README_SNIPPET = """# diko_kit（由 diko-use repair 拷入）
引用方式：
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "diko_kit"))  # 按实际调整
import diko_api_client as diko
access = diko.refresh_access()
```
首次使用：运行任一脚本时若 config/diko_api.env 为空，会交互式询问账号并保存。
红线：本目录 env 模板不得携带任何个人凭据对外分发。
"""


def _iter_text_files(root):
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn.endswith((".py", ".md", ".txt", ".env")):
                yield os.path.join(dirpath, fn)


def _scan(target: str):
    """在目标目录找各模块签名，返回 {module: [证据文件:行] }。"""
    evidence = {m: [] for m in MODULES}
    for path in _iter_text_files(target):
        if os.path.realpath(path).startswith(os.path.realpath(_HERE)):
            continue  # 不审计 diko-use 自身脚本
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        for m, spec in MODULES.items():
            if all(re.search(pat, text) for pat in spec["signatures"]):
                rel = os.path.relpath(path, target)
                if rel not in evidence[m]:
                    evidence[m].append(rel)
    return evidence


def cmd_self(_args):
    report = {m: {"desc": spec["desc"], "files": spec["files"]}
              for m, spec in MODULES.items()}
    print(json.dumps({"skill": "diko-use", "modules": report}, ensure_ascii=False, indent=2))
    print(f"\n本技能框架脚本齐全度：" +
          ("✅" if all(os.path.exists(os.path.join(_HERE, f)) for fs in MODULES.values() for f in fs["files"]) else "⚠️ 缺文件"))


def cmd_audit(args):
    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        raise SystemExit(f"目录不存在：{target}")
    ev = _scan(target)
    missing = [m for m, files in ev.items() if not files]
    report = {
        "target": target,
        "modules": {m: {"present": bool(files), "evidence": files[:3]} for m, files in ev.items()},
        "missing": missing,
        "next": (f"python doctor.py repair \"{target}\" --module all" if missing else "none"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not missing else 2


def _copy_into(fname: str, kit_dir: str, force: bool):
    src = os.path.join(_HERE, fname)
    if not os.path.exists(src):
        return ("missing-src", fname)
    dst = os.path.join(kit_dir, fname)
    if os.path.exists(dst):
        if not force:
            return ("skipped", fname)
        shutil.copy2(dst, dst + ".bak")
    shutil.copy2(src, dst)
    return ("copied" if not os.path.exists(dst + ".bak") else "overwrote+backup", fname)


def cmd_repair(args):
    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        raise SystemExit(f"目录不存在：{target}")
    ev = _scan(target)
    missing = [m for m, files in ev.items() if not files]
    if args.module == "all":
        want = missing
    else:
        want = [args.module]
        if args.module not in missing:
            print(json.dumps({"ok": True, "note": f"模块 {args.module} 已存在，无需维修",
                              "evidence": ev[args.module][:3]}, ensure_ascii=False))
            return
    # 依赖闭包
    expanded = set(want)
    for m in list(expanded):
        for dep in MODULES[m]["depends"]:
            if dep in missing:
                expanded.add(dep)
    want = [m for m in MODULES if m in expanded]

    kit_dir = os.path.join(target, "diko_kit")
    cfg_dir = os.path.join(kit_dir, "config")
    os.makedirs(cfg_dir, exist_ok=True)

    actions = []
    for m in want:
        for f in MODULES[m]["files"]:
            actions.append((m,) + _copy_into(f, kit_dir, args.force))
    env_path = os.path.join(cfg_dir, "diko_api.env")
    if os.path.exists(env_path) and not args.force:
        actions.append(("env", "skipped", "config/diko_api.env"))
    else:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(ENV_TEMPLATE)
        actions.append(("env", "created", "config/diko_api.env（全空模板）"))
    with open(os.path.join(kit_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(README_SNIPPET)

    print(json.dumps({"ok": True, "repaired_modules": want, "kit_dir": kit_dir,
                      "actions": [{"module": a, "action": b, "file": c} for a, b, c in actions],
                      "next": f"python doctor.py audit \"{target}\"  # 复验"},
                     ensure_ascii=False, indent=2))
    print("\n⚠️ 提醒：repair 完成后必须再次 audit 复验，并向使用者说明引用方式（见 diko_kit/README.md）。")


def build_parser():
    p = argparse.ArgumentParser(description="diko-use 自检维修")
    sub = p.add_subparsers(dest="cmd")
    p.add_argument("--self", action="store_true", dest="selfcheck", help="列出本技能模块")
    a = sub.add_parser("audit"); a.add_argument("target"); a.set_defaults(func=cmd_audit)
    r = sub.add_parser("repair")
    r.add_argument("target")
    r.add_argument("--module", required=True, choices=list(MODULES) + ["all"])
    r.add_argument("--force", action="store_true", help="覆盖已有文件（自动 .bak 备份）")
    r.set_defaults(func=cmd_repair)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if getattr(args, "selfcheck", False) or not getattr(args, "cmd", None):
        cmd_self(args)
        return
    rc = args.func(args)
    if isinstance(rc, int) and rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
