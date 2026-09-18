#!/usr/bin/env python3
"""
DIKO 门禁 · 完整性互锁校验（gate_kit 通用版，diko 防删核心）。

信任链（双向锁）：
  SKILL.md 中一行  `<!-- GATE_MANIFEST_SHA256: xxx -->`  ← 锁住 →  scripts/manifest.json
  manifest.json 内的 sha256 表                            ← 锁住 →  全部核心文件（含 SKILL.md 本身、
                                                            gate.py、diko_kit/* 等）

任何一个核心文件被删除/篡改/绕过（包括改 manifest.json 或改 SKILL.md 的锁行）都会 FAIL。
本文件自身也在清单内，删 integrity.py 同样 FAIL。

SKILL.md 哈希自洽：锁行值随清单变动，故对 SKILL.md 计算哈希前先把锁行归一化为
`GATE_MANIFEST_SHA256: LOCK_PLACEHOLDER`（与 make_manifest.py 完全一致），避免鸡生蛋死结。

用法：
  python integrity.py            # 校验，PASS 退出码 0，FAIL 退出码 1（打印缺失/损坏清单）
  python integrity.py --json     # 机器可读输出
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
MANIFEST_PATH = os.path.join(HERE, "manifest.json")
CFG_PATH = os.path.join(HERE, "gate_config.json")


def _gate_cfg():
    """可选 scripts/gate_config.json 调锁定范围：{"include_dirs": [...], "check_dirs": [...]}"""
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

SKILL_MD = os.path.join(SKILL_ROOT, "SKILL.md")
LOCK_RE = re.compile(r"GATE_MANIFEST_SHA256:\s*([0-9a-f]{64}|LOCK_PLACEHOLDER)")
LOCK_NORM = b"GATE_MANIFEST_SHA256: LOCK_PLACEHOLDER"


def normalized_sha(path, rel):
    data = open(path, "rb").read()
    if rel.replace(os.sep, "/") == "SKILL.md":
        data = re.sub(rb"GATE_MANIFEST_SHA256:\s*(?:[0-9a-f]{64}|LOCK_PLACEHOLDER)",
                      LOCK_NORM, data)
    return hashlib.sha256(data).hexdigest()


def load_manifest():
    if not os.path.isfile(MANIFEST_PATH):
        return None, "manifest.json 不存在（被删除）"
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"manifest.json 无法解析：{e}"


def check():
    """返回 (ok: bool, problems: list[str])"""
    problems = []

    # 1) 锁行：SKILL.md 必须存在且含 manifest 哈希锁
    if not os.path.isfile(SKILL_MD):
        return False, ["SKILL.md 不存在"]
    md_text = open(SKILL_MD, "r", encoding="utf-8").read()
    m = LOCK_RE.search(md_text)
    if not m:
        return False, ["SKILL.md 缺少 GATE_MANIFEST_SHA256 锁行（文档被重建或删除）"]
    if m.group(1) == "LOCK_PLACEHOLDER":
        return False, ["SKILL.md 锁行仍是占位符（清单未生成/未回写）"]
    lock_hash = m.group(1)

    # 2) manifest.json 必须与锁行一致（防伪造清单）
    man, err = load_manifest()
    if err:
        return False, [err]
    body = json.dumps(man, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != lock_hash:
        return False, ["manifest.json 与 SKILL.md 锁行不一致（清单被篡改）"]

    # 3) 清单内每个文件的哈希
    files = man.get("files", {})
    if not files:
        return False, ["manifest.json files 表为空"]
    for rel, want in sorted(files.items()):
        p = os.path.join(SKILL_ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            problems.append(f"缺失文件: {rel}")
            continue
        if normalized_sha(p, rel) != want:
            problems.append(f"哈希不符(被篡改/替换): {rel}")

    # 4) 目录结构存在（默认为门禁自身依赖，可 gate_config.json 扩充）
    check_dirs = _gate_cfg().get("check_dirs", ["diko_kit/scripts/config"])
    for d in check_dirs:
        if not os.path.isdir(os.path.join(SKILL_ROOT, d.replace("/", os.sep))):
            problems.append(f"缺失目录: {d.replace(os.sep, chr(47))}")

    return len(problems) == 0, problems


def main():
    as_json = "--json" in sys.argv
    ok, problems = check()
    if as_json:
        print(json.dumps({"ok": ok, "problems": problems}, ensure_ascii=False, indent=1))
    else:
        if ok:
            print("INTEGRITY: PASS")
        else:
            print("INTEGRITY: FAIL")
            for p in problems:
                print(" -", p)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
