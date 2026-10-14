#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
policy_lib.py —— 策略外置共享读取（Phase 0）
============================================
audit.py 与 qwjs_classify.py 共用本模块读取 policy.yaml，
避免规则散落、避免重复实现解析器。

零外部依赖：内置极简 YAML 解析（仅支持策略文件用到的子集：
嵌套 map / 标量列表 / 标量键值）。GitHub runner 无需 pip install。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT, os.getcwd()):
    if p not in sys.path:
        sys.path.insert(0, p)

DEFAULTS = {
    "audit": {"all_min": 800, "all_max": 3500, "part_min": 50, "part_max": 800},
    "audio_keywords": ["音乐", "听书", "喜马拉雅", "广播", "电台", "相声", "评书", "漫画", "电子书"],
    "video_keywords": ["直播", "戏曲", "电视", "体育", "哔哩", "动漫", "短剧", "电影", "综艺"],
    "user_remove": ["哔哩", "bilibili", "b站", "BILIBILI"],
}


def _parse_scalar(v):
    v = (v or "").strip()
    if v == "":
        return None
    if (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'"):
        return v[1:-1]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _strip_comment(line):
    s = line.strip()
    if s.startswith("#"):
        return ""
    m = re.search(r"\s#", line)
    if m:
        line = line[:m.start()].rstrip()
    return line


def _load_raw(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_policy(path=None):
    """读取 policy.yaml；缺失或解析失败回退 DEFAULTS（闸门永远在线）。"""
    candidates = [
        path,
        os.environ.get("POLICY_YAML"),
        os.path.join(ROOT, "policy.yaml"),
        os.path.join(HERE, "policy.yaml"),
        os.path.join(os.getcwd(), "policy.yaml"),
        "policy.yaml",
    ]
    text = None
    for c in candidates:
        if c and os.path.exists(c):
            try:
                text = _load_raw(c)
                break
            except Exception:
                continue
    if text is None:
        return {}

    raw_lines = []
    for ln in text.splitlines():
        s = _strip_comment(ln)
        if not s.strip():
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        raw_lines.append((indent, s.strip()))
    pos = [0]

    def parse_block(min_indent):
        result = None
        while pos[0] < len(raw_lines):
            indent, content = raw_lines[pos[0]]
            if indent < min_indent:
                break
            if content.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    break
                pos[0] += 1
                result.append(_parse_scalar(content[2:]))
            else:
                if result is None:
                    result = {}
                if not isinstance(result, dict):
                    break
                key, _, val = content.partition(":")
                key = key.strip()
                val = val.strip()
                pos[0] += 1
                if val == "":
                    result[key] = parse_block(indent + 1)
                else:
                    result[key] = _parse_scalar(val)
        return result

    return parse_block(0) or {}


def get_audit_bounds(policy):
    cfg = policy.get("audit", {}) or {}
    d = DEFAULTS["audit"]
    return {
        "all_min": cfg.get("all_min", d["all_min"]),
        "all_max": cfg.get("all_max", d["all_max"]),
        "part_min": cfg.get("part_min", d["part_min"]),
        "part_max": cfg.get("part_max", d["part_max"]),
    }


def get_list(policy, key):
    return [str(x) for x in (policy.get(key) or DEFAULTS.get(key, []))]
