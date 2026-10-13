#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS 订阅清理脚本（部署到 dearloyal/CS → scripts/cleanup.py）
==========================================================
2026-10-14 新增：把「今天手动清理的全部规则」固化成自动步骤，接进 pipeline，
避免每次人工排查（藤藤死源 / 非凡量子等重复马甲 / 音乐听书等音频源）。

清理规则（对 全部.json 与 部分.json 同时生效）：
  ① 删纯音频源：音乐 / 听歌 / KTV / 听书 / 有声 / 广播 / 电台 / 相声 / 评书 /
     radio / music / 网易云 / 千千音乐 / 枫叶音乐 / 悦听 / 音乐聚合 / DJ 等。
     —— 视频类（直播 / 戏曲 / 电视 / 体育 / 虎牙 / 哔哩 / 动漫 等）一律保留，
        视频关键词优先级高于音频关键词，避免误删。
  ② 同站马甲去重：对 type 0/1 的苹果CMS直连源，按「资源站品牌」归并
     （剥掉 api./cj./caiji. 前缀、合并同站别名域名，如 lziapi/lzizy/lzzy → 量子），
     同一资源站只留 1 个最优（type=1 + https + 标准 api.php/provide/vod + 探活存活优先）。
  ③ 名字保持「全部」/「部分」不变，同步 count 字段；脚本幂等，可重复跑。

与 probe.py 的分工：
  · probe 负责「死链检测 + 精确 api 去重 + 防抖暂留」；
  · 本脚本负责「语义级清理」（音频过滤 + 同站品牌去重），跑在 probe 之前，
    避免对注定要删的源白做探活、也避免把马甲源写进最终订阅。
"""
import json
import os
import re
import sys

# ── ① 音频关键词（命中即视为非影视，删除） ──────────────────────────────
AUDIO_KW = [
    "音乐", "听歌", "ktv", "KTV", "听书", "有声", "广播", "电台", "相声", "评书",
    "radio", "music", "音频", "爱听", "千千音乐", "网易云", "枫叶音乐", "悦听",
    "音乐聚合", "音乐网", "听书网", "DJ",
]
# ── 视频关键词（命中则保留，优先级高于音频，防误删直播/戏曲/动漫等） ──────
# 注意：本仓每个源名都带「影视 」前缀，故「影视」不参与判定（在 is_audio 内先剥离前缀）。
VIDEO_KW = [
    "直播", "戏曲", "电视", "体育", "猫TV", "JRKAN", "油管", "熊猫", "虎牙", "哔哩",
    "网红", "动漫", "短剧", "电影", "综艺", "剧场",
]

# ── ② 同站品牌映射（同站不同域名别名归并到同一资源站） ──────────────────
# 剥前缀后再取主域名第一段（去末尾数字），再经此表归一。
STATION_ALIAS = {
    "lzizy": "量子", "lzzy": "量子", "lziapi": "量子", "lzcaiji": "量子",
    "360zyzz": "360", "360zy": "360",
    "zuidapi": "最大", "zuidazy": "最大", "zuida": "最大",
    "ffzy": "非凡", "ffzyapi": "非凡", "ffzynet": "非凡",
    "wujinapi": "无尽",
    "xsd": "闪电", "shandianzy": "闪电", "sdzyapi": "闪电", "sdzy": "闪电",
    "by": "暴风", "bfzyapi": "暴风", "bfzy": "暴风",
    "ikunzy": "IKUN", "ikunzyapi": "IKUN", "ikun": "IKUN",
    "jinyingzy": "金鹰", "jyzyapi": "金鹰", "jyzy": "金鹰",
}
# 整组保留、不去重的特殊服务器（同一服务器上的不同应用，内容不同）
KEEP_ALL_STATIONS = {"zhangqun"}


def is_audio(name):
    """纯音频判定：含视频关键词则保留；否则含音频关键词即非影视。

    本仓源名统一带「影视 」前缀，先剥离再判，避免「影视」误触发视频保护。
    """
    name = re.sub(r"^影视\s*", "", str(name))
    if any(v in name for v in VIDEO_KW):
        return False
    return any(a.lower() in name.lower() for a in AUDIO_KW)


def host_of(api):
    m = re.match(r"^https?://([^/]+)", str(api))
    return m.group(1).lower() if m else ""


def station_of(api):
    """从 api 解析资源站品牌（用于同站马甲去重）。"""
    h = host_of(api)
    if not h:
        return ""
    h = re.sub(r"^www\.", "", h)
    h = re.sub(r"^(api|caiji|cj|json|at)\.", "", h)   # 剥采集前缀
    main = h.split(".")[0]
    main = re.sub(r"\d+$", "", main)                  # 去末尾数字（ffzy8→ffzy）
    return STATION_ALIAS.get(main, main)


def score(s):
    """同站去重时，挑最优源：type=1 + https + 标准接口 + 探活存活优先。"""
    api = str(s.get("api", ""))
    name = str(s.get("name", ""))
    key = str(s.get("key", ""))
    sc = 0
    if s.get("type") == 1:
        sc += 30
    if api.startswith("https"):
        sc += 5
    if re.search(r"/api\.php/provide/vod/?(\?ac=list)?$", api):
        sc += 25
    if "at/xml" in api:
        sc -= 8
    if "at/json" in api or "at/josn" in api:
        sc -= 5
    if "/from/" in api:
        sc += 2
    if any(t in api for t in ("seaxml", "apple_m3u8", "api_mac", "apijson")):
        sc -= 10
    if "ac=list" in api:
        sc += 3
    if s.get("_alive") is True:
        sc += 15
    ms = s.get("_ms")
    if isinstance(ms, (int, float)):
        sc += max(0, 15 - ms / 500)
    # 干扰标签（马甲/采集/直连/代理延迟/vpn 等）降权
    for bad in ["采集接口", "采集网", "直连", "(vpn)", "(切)", "直播",
                "4011ms", "242ms", "377ms", "171ms", "173ms", "86ms",
                "647ms", "668ms", "358ms", "1880ms"]:
        if bad in name or bad in key:
            sc -= 12
    if re.match(r"^\d+-", name) or re.match(r"^[0-9a-f]{8,}", key):
        sc -= 10
    return sc


def clean_file(path):
    if not os.path.exists(path):
        print(f"[cleanup] 缺少 {path}，跳过")
        return
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    sites = d.get("sites", [])
    before = len(sites)

    # ① 删纯音频
    audio_hit = [s for s in sites if is_audio(str(s.get("name", "")))]
    audio_names = {s.get("name") for s in audio_hit}
    rest = [s for s in sites if s.get("name") not in audio_names]

    # ② 同站马甲去重（仅 type 0/1 的 CMS 直连源参与）
    groups = {}
    for s in rest:
        if str(s.get("type")) in ("0", "1"):
            st = station_of(s.get("api"))
            if st:
                groups.setdefault(st, []).append(s)
    remove = set()
    for st, members in groups.items():
        if st in KEEP_ALL_STATIONS or len(members) < 2:
            continue
        ranked = sorted(members, key=score, reverse=True)
        best = ranked[0]
        for other in ranked[1:]:
            remove.add(id(other))
    deduped = [s for s in rest if id(s) not in remove]

    d["sites"] = deduped
    d["count"] = len(deduped)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

    print(f"[cleanup] {path:16s} 源 {before}→{len(deduped)} "
          f"（删音频 {len(audio_hit)} / 同站去重 {len(rest) - len(deduped)}），"
          f"名称保持「{d.get('name')}」")


def main():
    files = ["全部.json", "部分.json"]
    for fn in files:
        clean_file(fn)
    print("[cleanup] 完成（幂等，可重复执行）")


if __name__ == "__main__":
    main()
