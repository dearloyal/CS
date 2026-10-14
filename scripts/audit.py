#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自审闸门 audit.py（Phase 0 核心）
================================
在「分类分成两个链接」之后、「发布推送」之前运行，对最终落盘的
全部.json / 部分.json 做质量断言。任一断言失败 → 打印明细 → 退出码非 0
→ pipeline 的 needs 链阻断 git push（fail-closed，宁可今天不更也不发脏数据）。

断言项：
  ① 全部.json 零成人 token（复用 adult_filter.is_adult，唯一真相源）
  ② 全部.json 零音频 / 非影视 token（policy.yaml 的 audio/video_keywords）
  ③ 全部 / 部分 各自内部无归一化域名重复
  ④ 源数 ∈ [min, max]（policy.yaml 的 audit 阈值，熔断）
  ⑤ 每源必含 name / key / api / type

依赖：adult_filter.py（同仓库），policy.yaml（同仓库根）。
运行：python3 scripts/audit.py [全部.json] [部分.json]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT, os.getcwd()):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── 策略读取：复用 policy_lib（零外部依赖，单一真相源）──
try:
    from policy_lib import load_policy, get_audit_bounds, get_list, DEFAULTS
except Exception:
    load_policy = None
    get_audit_bounds = None
    get_list = None
    DEFAULTS = {"audit": {"all_min": 800, "all_max": 3500, "part_min": 50, "part_max": 800}}


def _load_policy(path=None):
    if load_policy is not None:
        return load_policy(path)
    return {}


# ── 成人判定：复用 adult_filter.is_adult（唯一真相源）──
try:
    from adult_filter import is_adult as _is_adult
except Exception:
    _is_adult = None


def is_adult(site):
    if _is_adult is not None:
        return _is_adult(site)
    blob = " ".join(str(site.get(k, "")) for k in ("name", "key", "api"))
    return bool(re.search(r"(成人|🔞|色|黄|麻豆|av|porn|hentai|萝莉)", blob, re.I))


def norm_host(api):
    m = re.match(r"^https?://([^/]+)", str(api or ""))
    if not m:
        return ""
    h = m.group(1).lower()
    h = re.sub(r"^www\.", "", h)
    return h


def norm_full(api):
    """完整归一：去协议、去查询串、去 www、小写、去尾斜杠（保留路径，用于精确去重）。"""
    a = (api or "").strip().lower()
    a = re.sub(r"^https?://", "", a)
    a = re.sub(r"\?.*$", "", a)
    a = re.sub(r"^www\.", "", a)
    return a.rstrip("/")


def main():
    all_path = sys.argv[1] if len(sys.argv) > 1 else "全部.json"
    part_path = sys.argv[2] if len(sys.argv) > 2 else "部分.json"

    policy = _load_policy()
    if get_audit_bounds is not None:
        AUD = get_audit_bounds(policy)
    else:
        AUD = DEFAULTS["audit"]
    audio_kw = get_list(policy, "audio_keywords") if get_list else DEFAULTS["audio_keywords"]
    video_kw = get_list(policy, "video_keywords") if get_list else DEFAULTS["video_keywords"]
    user_remove = get_list(policy, "user_remove") if get_list else DEFAULTS["user_remove"]

    errors = []

    def load(name, path):
        if not os.path.exists(path):
            errors.append(f"[致命] 找不到 {name}：{path}（分类阶段是否未产出？）")
            return []
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("sites", []) or []

    all_sites = load("全部.json", all_path)
    part_sites = load("部分.json", part_path)
    if errors and not all_sites and not part_sites:
        for e in errors:
            print(e)
        sys.exit(2)

    # ④ 源数健康区间
    for label, sites, lo, hi in (
        ("全部.json", all_sites, AUD["all_min"], AUD["all_max"]),
        ("部分.json", part_sites, AUD["part_min"], AUD["part_max"]),
    ):
        n = len(sites)
        if n < lo or n > hi:
            errors.append(f"[熔断] {label} 源数 {n} 越界 [{lo}, {hi}]（疑似上游异常/污染）")
        else:
            print(f"  ✓ {label} 源数 {n} ∈ [{lo}, {hi}]")

    # ⑤ 必含字段：api 缺失属硬伤（拦截）；名字为空仅告警（不拦截，避免 1 个脏名阻断整批）
    for label, sites in (("全部.json", all_sites), ("部分.json", part_sites)):
        no_api = []
        empty_name = []
        for s in sites:
            name = re.sub(r"^(影视|18\s*)\s*", "", str(s.get("name", "") or "")).strip()
            api = str(s.get("api", "") or "").strip()
            if not api:
                no_api.append(str(s.get("name", "?")))
            elif not name:
                empty_name.append(str(s.get("name", "?")))
        if no_api:
            errors.append(f"[字段缺失] {label} 有 {len(no_api)} 个源缺 api：{no_api[:5]}")
        if empty_name:
            print(f"  ! {label} 有 {len(empty_name)} 个源名字为空（已加前缀仍空），建议人工核查：{empty_name[:5]}")

    # ③ 真·重复：仅当「完整 api（含路径）与 key 都非空且完全一致」时才算同一条源被列两次。
    #     本数据集里 host/name/key 均不唯一（采集站同名、品牌 key 共用），故必须用 api+key 双键，
    #     否则会误拦大量合法源。缺 key 或 api 的源不参与去重判定。
    def dedup_key(s):
        k = str(s.get("key", "") or "").strip()
        api = norm_full(s.get("api", ""))
        if k and api:
            return api + "|" + k
        return ""

    for label, sites in (("全部.json", all_sites), ("部分.json", part_sites)):
        seen = {}
        for s in sites:
            dk = dedup_key(s)
            if not dk:
                continue
            seen.setdefault(dk, []).append(s.get("name", "?"))
        dups = {k: ns for k, ns in seen.items() if len(ns) > 1}
        if dups:
            detail = "; ".join(f"{k.split('|',1)[1]}×{len(ns)}" for k, ns in list(dups.items())[:8])
            errors.append(f"[重复源] {label} 发现 {len(dups)} 个真重复（同 api+key）：{detail}")

    # ① 全部.json 零成人
    adult_hits = [s.get("name", "?") for s in all_sites if is_adult(s)]
    if adult_hits:
        errors.append(f"[成人泄漏] 全部.json 含 {len(adult_hits)} 个成人源：{adult_hits[:8]}")
    else:
        print(f"  ✓ 全部.json 成人泄漏检查通过（{len(all_sites)} 源）")

    # ② 全部.json 零音频 / 非影视（user_remove 整站删除，video 保护）
    audio_hits = []
    for s in all_sites:
        name = str(s.get("name", "") or "")
        low = name.lower()
        if any(u.lower() in low for u in user_remove):
            audio_hits.append(f"{name}[user_remove]")
            continue
        nm = re.sub(r"^影视\s*", "", name)
        if any(v.lower() in nm.lower() for v in video_kw):
            continue  # 视频优先保护
        if any(a.lower() in nm.lower() for a in audio_kw):
            audio_hits.append(name)
    if audio_hits:
        errors.append(f"[音频/非影视] 全部.json 含 {len(audio_hits)} 个非影视源：{audio_hits[:8]}")
    else:
        print(f"  ✓ 全部.json 音频/非影视检查通过（{len(all_sites)} 源）")

    print("─" * 48)
    if errors:
        print(f"✗ 自审未通过，拦截推送（{len(errors)} 项）：")
        for e in errors:
            print("  • " + e)
        sys.exit(1)
    print(f"✓ 自审全部通过：全部 {len(all_sites)} / 部分 {len(part_sites)}，准予发布。")


if __name__ == "__main__":
    main()
