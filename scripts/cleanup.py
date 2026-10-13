#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS 订阅清理脚本（部署到 dearloyal/CS → scripts/cleanup.py）
==========================================================
把「人工排查的全部规则」固化成自动步骤，接进 pipeline，避免每次手工清理。

清理规则（对 全部.json 与 部分.json 同时生效）：
  「成人内容」从 全部.json 移除后，自动「移入」部分.json（成人订阅），而非直接丢弃。
  ① 删纯音频源：音乐 / 听歌 / KTV / 听书 / 有声 / 广播 / 电台 / 相声 / 评书 /
     radio / music / 网易云 / 千千音乐 / 枫叶音乐 / 悦听 / 音乐聚合 / DJ /
     听小说 / 漫画 / 绘本 / 书城 / 电子书 等。视频类（直播 / 戏曲 / 动漫 等）保留。
  ② 删「藤藤 / 谐音」源：藤、腾、滕、疼、填、瑅 任一命中即删（均为依赖第三方解析、
     易失效的爬虫源；用户确认不可用）。
  ③ 成人内容（仅 全部.json 判定）：命中者从 全部.json 移除，并自动「移入」部分.json
     （成人订阅，统一加 18💥/18🔞 前缀），而非直接删除。用户点名的 鲨鱼/骚女/瘙女/樱花/
     百花/幸福/私人资源/国产资源/剧屋/isese/93合1 + 强色情关键词（无码/麻豆/探花/换妻/乱伦/人妻熟女…）。
     ★ 刻意排除「写真/人妻/熟女/少妇/丝袜」作分类级判定（普通电影站也含这些分类，
       会误删），它们只在「源名」里出现时才删。
  ④ 同站马甲去重：
     - type 0/1：按资源站品牌归并（剥 api./cj. 前缀、合并同站别名域名），同站留 1 个最优。
     - type 3：按「规则文件」归并（ext 非空则取 ext，否则取 api），同规则留 1 个。
       ★ 排除空 ext 的源（467 个空 ext 源 api 各不相同，不是重复，不能删）。
  ⑤ 名字保持「全部」/「部分」不变，同步 count；脚本幂等，可重复跑。

与 probe.py 的分工：probe 负责死链检测 + 防抖暂留；本脚本负责语义级清理，
跑在 probe 之前，避免对注定要删的源白做探活。
"""
import json
import os
import re
import sys

# ── ① 音频关键词（命中即非影视，删除；先剥离「影视 」前缀再判，避免误触）──
AUDIO_KW = [
    "音乐", "听歌", "ktv", "KTV", "听书", "有声", "广播", "电台", "相声", "评书",
    "radio", "music", "音频", "爱听", "千千音乐", "网易云", "枫叶音乐", "悦听",
    "音乐聚合", "音乐网", "听书网", "DJ", "听小说", "漫画", "绘本", "书城", "电子书",
]
# 视频关键词（命中则保留，优先级高于音频，防误删直播/戏曲/动漫等）
VIDEO_KW = [
    "直播", "戏曲", "电视", "体育", "猫TV", "JRKAN", "油管", "熊猫", "虎牙", "哔哩",
    "网红", "动漫", "短剧", "电影", "综艺", "剧场",
]

# ── ② 藤藤 / 谐音（均不可用，删除）──
TENG_HOMOPHONE = list("藤腾滕疼填瑅")

# ── ③ 成人内容（仅 全部.json 生效）──
# 用户点名（名字子串匹配，忽略大小写）
ADULT_NAMES = ["鲨鱼", "骚女", "瘙女", "樱花", "百花", "幸福", "私人资源", "国产资源", "剧屋", "isese"]
# 强色情「源名」token（命中源名即删；不含写真/人妻/熟女/少妇/丝袜这类易误报词）
ADULT_NAME_HARD = [
    "蜜桃", "乱伦", "人妻", "熟女", "少妇", "丝袜", "性爱", "黑料", "探花", "换妻",
    "群p", "群P", "群交", "无码", "有码", "麻豆", "精东", "自拍", "偷拍", "强奸",
    "迷奸", "调教", "巨乳", "美乳", "口交", "性奴", "中出", "潮吹", "足浴", "风俗",
    "素人", "写真", "伦理三级", "三级伦理", "成人频道", "福利姬", "麻豆传媒",
    "天美传媒", "星空传媒", "乌鸦传媒", "兔子先生", "海角社区", "海角乱伦",
    "91制片", "91黑料", "国产色情", "亚洲情色", "欧美性爱", "制服诱惑", "强制侵犯",
    "萝莉少女", "丝袜制服", "人妻熟女", "巨乳美乳",
    # ── 用户点名轮次新增（淫兽 / 9g88x / 玩蛋吧 / jk领域 / 大地资源）──
    # 关键泛化：单字「淫」可同时覆盖 淫兽穴院 / 淫水洞 / 京东淫联 / 淫母原味 等同族源
    "淫", "玩蛋吧", "大地资源", "jk领域", "JK领域", "9g88x", "93合1",
]
# 强色情「分类」token（仅当出现在 categories 字段时删；刻意排除写真/人妻/熟女/少妇/丝袜）
ADULT_CAT_HARD = [
    "无码", "有码", "麻豆", "精东", "蜜桃", "果冻", "星空", "AV解说", "自拍偷拍",
    "偷拍自拍", "强奸", "迷奸", "乱伦", "丝袜制服", "人妻熟女", "萝莉少女",
    "巨乳美乳", "口交", "群交", "群P", "群p", "换妻", "探花", "性爱", "裸聊",
    "欧美性爱", "亚洲情色", "国产色情", "淫妻", "制服诱惑", "强制侵犯", "足浴",
    "风俗", "素人特摄", "中出", "潮吹", "三级伦理", "伦理三级", "成人频道", "18禁",
    "福利姬", "麻豆传媒", "91制片", "天美传媒", "果冻传媒", "星空传媒", "乌鸦传媒",
    "兔子先生", "海角社区", "性奴", "调教",
]

# ── ④ 同站品牌映射（同站不同域名别名归并）──
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
    name = re.sub(r"^影视\s*", "", str(name))
    if any(v in name for v in VIDEO_KW):
        return False
    return any(a.lower() in name.lower() for a in AUDIO_KW)


def has_teng(name, key):
    s = str(name) + str(key)
    return any(ch in s for ch in TENG_HOMOPHONE)


def is_adult(name, cats):
    nm = str(name)
    low = nm.lower()
    if any(n.lower() in low for n in ADULT_NAMES):
        return True
    if any(t.lower() in low for t in ADULT_NAME_HARD):
        return True
    cstr = ""
    if isinstance(cats, list):
        cstr = " ".join(str(c) for c in cats)
    elif cats:
        cstr = str(cats)
    if any(t in cstr for t in ADULT_CAT_HARD):
        return True
    return False


def host_of(api):
    m = re.match(r"^https?://([^/]+)", str(api))
    return m.group(1).lower() if m else ""


def station_of(api):
    h = host_of(api)
    if not h:
        return ""
    h = re.sub(r"^www\.", "", h)
    h = re.sub(r"^(api|caiji|cj|json|at)\.", "", h)
    main = h.split(".")[0]
    main = re.sub(r"\d+$", "", main)
    return STATION_ALIAS.get(main, main)


def norm_ext(ext):
    ext = str(ext or "").strip()
    if not ext:
        return ""
    ext = re.sub(r"^https?://(gh-proxy\.com|ghp\.ci|ghproxy\.(net|cn)|mirror\.ghproxy\.com|cdn\.jsdelivr\.net/gh)/", "", ext)
    return ext.rstrip("/")


def score(s):
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
    for bad in ["采集接口", "采集网", "直连", "(vpn)", "(切)", "直播",
                "4011ms", "242ms", "377ms", "171ms", "173ms", "86ms",
                "647ms", "668ms", "358ms", "1880ms"]:
        if bad in name or bad in key:
            sc -= 12
    if re.match(r"^\d+-", name) or re.match(r"^[0-9a-f]{8,}", key):
        sc -= 10
    return sc


def clean_file(path, is_main, moved=None):
    if not os.path.exists(path):
        print(f"[cleanup] 缺少 {path}，跳过")
        return
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    sites = d.get("sites", [])
    before = len(sites)

    n_audio = n_teng = n_adult = n_dup = 0

    # ① 音频
    rest = []
    for s in sites:
        if is_audio(s.get("name", "")):
            n_audio += 1
        else:
            rest.append(s)

    # ② 藤藤 / 谐音
    rest2 = []
    for s in rest:
        if has_teng(s.get("name", ""), s.get("key", "")):
            n_teng += 1
        else:
            rest2.append(s)

    # ③ 成人（仅全部.json 判定；命中者移入部分.json，见 main）
    rest3 = []
    if is_main:
        for s in rest2:
            if is_adult(s.get("name", ""), s.get("categories")):
                n_adult += 1
                if moved is not None:
                    moved.append(s)
            else:
                rest3.append(s)
    else:
        rest3 = rest2

    # ④ 同站 / 同规则 去重
    groups = {}
    for s in rest3:
        t = str(s.get("type"))
        if t in ("0", "1"):
            st = station_of(s.get("api"))
            if st:
                groups.setdefault(("01", st), []).append(s)
        elif t == "3":
            rule = norm_ext(s.get("ext"))
            if not rule:                      # 空 ext：用 api（规则文件）作 key
                rule = norm_ext(s.get("api"))
            if rule:
                groups.setdefault(("3", rule), []).append(s)
    remove = set()
    for (kind, key), members in groups.items():
        if len(members) < 2:
            continue
        # 同服务器多应用整组保留
        if kind == "01" and key in KEEP_ALL_STATIONS:
            continue
        ranked = sorted(members, key=score, reverse=True)
        for other in ranked[1:]:
            remove.add(id(other))
    deduped = [s for s in rest3 if id(s) not in remove]
    n_dup = len(rest3) - len(deduped)

    d["sites"] = deduped
    d["count"] = len(deduped)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

    print(f"[cleanup] {os.path.basename(path):16s} 源 {before}→{len(deduped)} "
          f"（删音频 {n_audio} / 藤藤谐音 {n_teng} / 成人(移入部分) {'%d' % n_adult if is_main else '—(跳过)'} / 去重 {n_dup}），"
          f"名称保持「{d.get('name')}」")


def move_name(site, existing_names):
    """成人源从全部.json移入部分.json时，按类型统一加 18💥/18🔞 前缀。"""
    n = str(site.get("name", ""))
    for pre in ["影视 💥", "影视 🌈", "影视 ", "18💥", "18🔞", "💥"]:
        n = n.replace(pre, "")
    n = n.strip()
    nm = ("18💥" + n + "💥") if str(site.get("type")) == "3" else ("18🔞" + n)
    if nm in existing_names:
        nm = f"{nm}（{site.get('key')}）"
    return nm


def main():
    files = sys.argv[1:] or ["全部.json", "部分.json"]
    moved = []
    for fn in files:
        is_main = ("全部" in fn) and ("部分" not in fn)
        clean_file(fn, is_main, moved if is_main else None)
    # 把全部.json 中移除的成人源，移入部分.json（成人订阅）
    if moved:
        part_fn = "部分.json"
        if os.path.exists(part_fn):
            with open(part_fn, encoding="utf-8") as f:
                pd = json.load(f)
            psites = pd.get("sites", [])
            pkeys = {s.get("key") for s in psites}
            pnames = {s.get("name") for s in psites}
            add = 0
            for s in moved:
                if s.get("key") in pkeys:
                    continue
                s = dict(s)
                s["name"] = move_name(s, pnames)
                psites.append(s)
                pnames.add(s["name"])
                pkeys.add(s["key"])
                add += 1
            pd["sites"] = psites
            pd["count"] = len(psites)
            with open(part_fn, "w", encoding="utf-8") as f:
                json.dump(pd, f, ensure_ascii=False, indent=2)
            print(f"[cleanup] 移入部分.json 成人源 {add} 个（部分.json 现 {len(psites)}）")
    print("[cleanup] 完成（幂等，可重复执行）")


if __name__ == "__main__":
    main()
