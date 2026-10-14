#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS 全量清理脚本（部署到 dearloyal/CS → scripts/cleanup.py）
==========================================================
蓝图 v2.1 定位：作为「分类拆分前」的全量清理阶段（架构图里的 1.5 位置），
对 ysc_build 产出的统一数据池 网站接口数据.json 做一次性清理，classify 随后
把清理后的池一次性切成「全部 / 部分」两个链接。实现「先清理、后分链」。

清理规则（全部来自 policy.yaml 单一真相源，改规则无需改代码）：
  ⓪ 用户点名删除（block_names，优先级最高）：如 酷我。
  ① 用户整站删除（user_remove）：如 哔哩 / bilibili / b站。
  ② 删纯音频 / 非影视源（audio_keywords 命中，先剥离「影视 」前缀，遇 video_keywords 则保留）。
  ③ 删藤藤 / 谐音源（teng_homophone 任一命中即删；手机端不可用）。
  ④ 删「需插件 / 引擎」源：api 非 http（csp_/相对路径/jar:/js:/py:）或命中 spider/csp_/
     engine/drpy/http 脚本——影视仓在手机上无法播放，直接丢弃（即用户说的「爬虫类不能用的」）。
  ⑤ 同站 / 同规则 去重（station_alias 归并同站别名，同站留 1 个最优；type3 按规则文件归并）。

与 qwjs_classify 的分工：本脚本在「拆分前」完成全部语义级清理与去重；
qwjs_classify 仅负责「拆分」（成人分流 + 分类缓存 + 标志 + 置顶 + 排序）。
成人判定由 adult_filter.is_adult（单一真相源）在 classify 阶段完成，不在此处。

输入：网站接口数据.json（yscall build 产出，含 {"sites": [...]}）
输出：原地写回网站接口数据.json（清理后的统一池），供 classify 读取。
幂等，可重复执行。
"""
import json
import os
import re
import sys

# ── 策略读取：复用 policy_lib（零外部依赖，单一真相源）──
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ROOT, os.getcwd()):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from policy_lib import load_policy as _load_policy, get_list as _get_list
    _POL = _load_policy()
    _OK = True
except Exception as _e:
    _POL = {}
    _OK = False
    print(f"[cleanup] 警告：policy.yaml 加载失败（{_e}），回退内置默认值。")

# ── 默认值（policy.yaml 缺失时的兜底，保证闸门永远在线）──
_DEF_AUDIO = ["音乐", "听歌", "ktv", "KTV", "听书", "有声", "广播", "电台", "相声", "评书",
              "radio", "music", "音频", "爱听", "千千音乐", "网易云", "枫叶音乐", "悦听",
              "音乐聚合", "音乐网", "听书网", "DJ", "听小说", "漫画", "绘本", "书城", "电子书", "喜马拉雅"]
_DEF_VIDEO = ["直播", "戏曲", "电视", "体育", "猫TV", "JRKAN", "油管", "熊猫", "虎牙", "哔哩",
              "网红", "动漫", "短剧", "电影", "综艺", "剧场"]
_DEF_USER_REMOVE = ["哔哩", "bilibili", "b站", "BILIBILI"]
_DEF_TENG = list("藤腾滕疼填瑅")
_DEF_BLOCK = ["酷我"]
_DEF_STATION = {
    "lzizy": "量子", "lziapi": "量子", "lzcaiji": "量子", "lzzy": "量子",
    "360zyzz": "360", "360zy": "360",
    "zuidapi": "最大", "zuidazy": "最大", "zuida": "最大",
    "ffzy": "非凡", "ffzyapi": "非凡", "ffzynet": "非凡",
    "wujinapi": "无尽",
    "xsd": "闪电", "shandianzy": "闪电", "sdzyapi": "闪电", "sdzy": "闪电",
    "by": "暴风", "bfzyapi": "暴风", "bfzy": "暴风",
    "ikunzy": "IKUN", "ikunzyapi": "IKUN", "ikun": "IKUN",
    "jinyingzy": "金鹰", "jyzyapi": "金鹰", "jyzy": "金鹰",
}
_DEF_KEEP_ALL = {"zhangqun"}

AUDIO_KW = _get_list(_POL, "audio_keywords") if _OK else _DEF_AUDIO
VIDEO_KW = _get_list(_POL, "video_keywords") if _OK else _DEF_VIDEO
USER_REMOVE = _get_list(_POL, "user_remove") if _OK else _DEF_USER_REMOVE
BLOCK_NAMES = _get_list(_POL, "block_names") if _OK else _DEF_BLOCK
TENG_HOMOPHONE = list(str(_POL.get("teng_homophone", "".join(_DEF_TENG))))
STATION_ALIAS = (_POL.get("station_alias") or _DEF_STATION) if _OK else _DEF_STATION
KEEP_ALL_STATIONS = set(_get_list(_POL, "keep_all_stations") or _DEF_KEEP_ALL)


def is_blocked(name):
    """用户点名删除（最高优先级）。"""
    nm = str(name)
    return any(b and b in nm for b in BLOCK_NAMES)


def is_user_remove(name):
    low = str(name).lower()
    return any(u.lower() in low for u in USER_REMOVE)


def is_audio(name):
    """命中音频词即非影视；先剥离「影视 」前缀，遇视频词则保留（防误删直播/戏曲等）。"""
    name = re.sub(r"^影视\s*", "", str(name))
    if any(v in name for v in VIDEO_KW):
        return False
    return any(a.lower() in name.lower() for a in AUDIO_KW)


def has_teng(name, key):
    s = str(name) + str(key)
    return any(ch in s for ch in TENG_HOMOPHONE)


def is_plugin_source(s):
    """该源是否需要 spider.jar / 引擎才能在手机上播放（用户说的「爬虫类不能用的」）。

    信号在 ext / api 是否指向 spider 或引擎脚本，而非 type 字段
    （上游把大量可用标准接口也标成了 type=1）。命中即视为手机端不可用，直接丢弃。
    """
    ext = str(s.get("ext", "") or "").lower()
    api = str(s.get("api", "") or "").lower()
    blob = ext + " " + api
    if not api.startswith("http"):          # 非 http：csp_/相对路径/jar:/js:/py: 引擎地址
        return True
    if ext.startswith("jar:") or ext.startswith("py:") or ext.startswith("js:") or "js:" in blob:
        return True
    if "spider" in blob:
        return True
    if "csp_" in api:                       # 引擎标识
        return True
    if "engine" in blob or "drpy" in blob or "dr_py" in blob:
        return True
    if ext and (ext.endswith(".js") or "/js/" in ext or (ext.endswith(".js") and "github" in ext)):
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
    """同站去重时挑「最优」保留：标准 CMS 接口、https、type=1 优先。"""
    api = str(s.get("api", ""))
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
        if bad in str(s.get("name", "")) or bad in key:
            sc -= 12
    if re.match(r"^\d+-", str(s.get("name", ""))) or re.match(r"^[0-9a-f]{8,}", key):
        sc -= 10
    return sc


def clean_pool(sites):
    """对统一数据池做拆分前全量清理，返回 (保留列表, 计数 dict)。"""
    rm = {"block": 0, "user_remove": 0, "audio": 0, "teng": 0, "plugin": 0}

    # ⓪ 用户点名删除（最高优先级）
    rest = []
    for s in sites:
        if is_blocked(s.get("name", "")):
            rm["block"] += 1
        else:
            rest.append(s)

    # ① 用户整站删除
    rest1 = []
    for s in rest:
        if is_user_remove(s.get("name", "")):
            rm["user_remove"] += 1
        else:
            rest1.append(s)

    # ② 音频 / 非影视
    rest2 = []
    for s in rest1:
        if is_audio(s.get("name", "")):
            rm["audio"] += 1
        else:
            rest2.append(s)

    # ③ 藤藤 / 谐音
    rest3 = []
    for s in rest2:
        if has_teng(s.get("name", ""), s.get("key", "")):
            rm["teng"] += 1
        else:
            rest3.append(s)

    # ④ 需插件 / 引擎 源（手机端不可用）
    kept = []
    for s in rest3:
        if is_plugin_source(s):
            rm["plugin"] += 1
        else:
            kept.append(s)

    # ⑤ 同站 / 同规则 去重
    groups = {}
    for s in kept:
        t = str(s.get("type"))
        if t in ("0", "1"):
            st = station_of(s.get("api"))
            if st:
                groups.setdefault(("01", st), []).append(s)
        elif t == "3":
            rule = norm_ext(s.get("ext"))
            if not rule:
                rule = norm_ext(s.get("api"))
            if rule:
                groups.setdefault(("3", rule), []).append(s)
    remove = set()
    for (kind, key), members in groups.items():
        if len(members) < 2:
            continue
        if kind == "01" and key in KEEP_ALL_STATIONS:
            continue
        ranked = sorted(members, key=score, reverse=True)
        for other in ranked[1:]:
            remove.add(id(other))
    deduped = [s for s in kept if id(s) not in remove]
    rm["dup"] = len(kept) - len(deduped)

    return deduped, rm


def main():
    inp = "网站接口数据.json"
    if not os.path.exists(inp):
        print(f"[cleanup] 找不到 {inp}（请先运行 ysc_build 产出统一池），跳过。")
        return
    with open(inp, encoding="utf-8") as f:
        d = json.load(f)
    sites = d.get("sites", [])
    print(f"[cleanup] 读取统一池源数: {len(sites)}")

    cleaned, rm = clean_pool(sites)
    if any(rm.values()):
        print(f"  丢弃：点名删除 {rm['block']} | 用户删除 {rm['user_remove']} | "
              f"音频/非影视 {rm['audio']} | 藤谐音 {rm['teng']} | "
              f"插件/引擎(不可用) {rm['plugin']} | 同站去重 {rm['dup']}")

    d["sites"] = cleaned
    if "count" in d:
        d["count"] = len(cleaned)
    with open(inp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"[cleanup] 清理完成，统一池剩余 {len(cleaned)} 源（已写回 {inp}，供 classify 读取）。")


if __name__ == "__main__":
    main()
