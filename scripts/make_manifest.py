#!/usr/bin/env python3
"""
DIKO 门禁清单生成器（gate_kit 通用版）：改动任何核心文件后必须重跑本脚本，再把打印出的
哈希手工替换进 SKILL.md 的 GATE_MANIFEST_SHA256 锁行（脚本会尝试自动替换，
若失败按输出手工处理）。

manifest 排除清单：.gate_ok、raw_refrence/**、task_out/**、config/diko_api.env（含真实凭据，
用户私有内容不入锁；但 env 文件被删同样导致 diko 无法工作，属运行期问题非完整性问题）。
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
CFG_PATH = os.path.join(HERE, "gate_config.json")


def _gate_cfg():
    """可选 scripts/gate_config.json 调锁定范围：{"include_dirs": [...], "check_dirs": [...]}"""
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


INCLUDE_DIRS = _gate_cfg().get("include_dirs", ["scripts", "diko_kit", "references"])
INCLUDE_FILES = ["SKILL.md"]
EXCLUDE_NAMES = {"make_manifest.py", "__pycache__"}
EXCLUDE_PATHS = {os.path.join("diko_kit", "scripts", "config", "diko_api.env"),
                 os.path.join("scripts", "manifest.json")}  # 自引用悖论：manifest 由 SKILL.md 锁行直接保护


def sha256_obj(obj):
    body = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build():
    files = {}
    for rel in INCLUDE_FILES:
        p = os.path.join(SKILL_ROOT, rel)
        if os.path.isfile(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                data = f.read()
            # 锁行自身不参与本文件哈希计算前的替换：占位为固定串，保证幂等
            data = re.sub(rb"GATE_MANIFEST_SHA256:\s*[0-9a-f]{64}",
                          b"GATE_MANIFEST_SHA256: LOCK_PLACEHOLDER", data)
            h.update(data)
            files[rel] = h.hexdigest()
    for d in INCLUDE_DIRS:
        base = os.path.join(SKILL_ROOT, d)
        for root, dirs, names in os.walk(base):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for n in names:
                if n in EXCLUDE_NAMES or n.endswith((".pyc", ".lock")):
                    continue
                full = os.path.join(root, n)
                rel = os.path.relpath(full, SKILL_ROOT).replace(os.sep, "/")
                if rel.replace("/", os.sep) in EXCLUDE_PATHS:
                    continue
                fp = hashlib.sha256(open(full, "rb").read()).hexdigest()
                files[rel] = fp
    man = {"version": 1, "generated_by": "make_manifest.py", "files": files}
    return man


def main():
    man = build()
    # manifest.json 自身在写入前后哈希不同，故不列入清单（它由 SKILL.md 锁行直接保护）
    mp = os.path.join(HERE, "manifest.json")
    with open(mp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(man, f, ensure_ascii=False, indent=2, sort_keys=True)
    lock = sha256_obj(man)

    # 自动回写 SKILL.md 锁行
    sk = os.path.join(SKILL_ROOT, "SKILL.md")
    text = open(sk, "r", encoding="utf-8").read()
    new, n = re.subn(r"GATE_MANIFEST_SHA256:\s*(LOCK_PLACEHOLDER|[0-9a-f]{64})",
                     f"GATE_MANIFEST_SHA256: {lock}", text)
    if n == 1:
        open(sk, "w", encoding="utf-8", newline="\n").write(new)
    print(json.dumps({"manifest": mp, "lock_sha256": lock,
                      "skill_md_lock_updated": n == 1,
                      "files": len(man["files"])}, ensure_ascii=False, indent=1))
    if n != 1:
        print("!! 请手工把 lock_sha256 写入 SKILL.md 锁行", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
