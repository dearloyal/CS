#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QWJS 分类脚本（部署到 dearloyal/CS → scripts/qwjs_classify.py）
==============================================================
任务（按用户 2026-10-11 要求重构）：
  读取 YSC 产出的「全部网站接口」（网站接口数据.json），统一输出 2 份订阅文件：
        全部.json : 正常可用接口（直连 + 爬虫 合并）
        部分.json : 成人接口（直连 + 爬虫 合并）
  不再区分直连 / 爬虫（用户要求合并）。

引擎源处理（2026-10-13 更新）：
  旧逻辑把引擎源留在 全部.json 里、probe 阶段再整体丢弃，用户感知「源少了一半」。
  后改为预分流到 候选.json（候选层），但实测引擎源在 App 内基本不可用，
  故 2026-10-13 起彻底废弃候选层：非 http 接口（csp_/相对路径/jar:/js:/py:）
  的引擎源直接丢弃，不进入 全部/部分，也不落候选层。

附加处理：
  ① 删除「酷我」系列（用户指定从全部中移除）；
  ② 给每个源打标志：正常 → 名字前缀「影视 」，成人 → 名字前缀「18 」；
  ③ 剔除「验证为空」的分类（empty_cats_cache.json，由 probe_empty_cats.py 逐源探测
     ?ac=videolist&t=<type_id> 的 total 生成；苹果CMS 筛选参数是 t 不是 type）；
  ④ 排序：全部.json 把「❤闪电资源」（心型闪电，有分类）置顶；部分.json 不强制置顶，纯按「分类【能显示】在前、不能显示在后」排序。
     「能显示」= 有真实分类 且 type != 0（用户确认 type=0 源不显示分类筛选栏）。

成人判定与 build 阶段共用 adult_filter.is_adult，规则唯一、不漂移。
运行：python3 scripts/qwjs_classify.py
"""
import json, os, re, sys, time, subprocess, datetime

# ── Phase 0 策略外置：音频 / 用户指定删除 / 藤谐音 过滤（在分类拆分前执行 = 蓝图 1.5 位置）──
# 规则来自 policy.yaml（单一真相源），改规则无需改代码。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "scripts"), os.getcwd()):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from policy_lib import load_policy as _load_policy, get_list as _get_list
    _POL = _load_policy()
    AUDIO_KW = _get_list(_POL, "audio_keywords")
    VIDEO_KW = _get_list(_POL, "video_keywords")
    USER_REMOVE = _get_list(_POL, "user_remove")
except Exception:
    AUDIO_KW = ["音乐", "听书", "喜马拉雅", "广播", "电台", "相声", "评书", "漫画", "电子书"]
    VIDEO_KW = ["直播", "戏曲", "电视", "体育", "哔哩", "动漫", "短剧", "电影", "综艺"]
    USER_REMOVE = ["哔哩", "bilibili", "b站", "BILIBILI"]
TENG_HOMOPHONE = list("藤腾滕疼填瑅")


def is_audio(name):
    """命中音频词即非影视；先剥离「影视 」前缀，遇视频词则保留（防误删直播/戏曲等）。"""
    name = re.sub(r"^影视\s*", "", str(name))
    if any(v in name for v in VIDEO_KW):
        return False
    return any(a.lower() in name.lower() for a in AUDIO_KW)


def is_user_remove(name):
    low = str(name).lower()
    return any(u.lower() in low for u in USER_REMOVE)


def has_teng(name, key):
    s = str(name) + str(key)
    return any(ch in s for ch in TENG_HOMOPHONE)


def is_plugin_source(s):
    """该源是否需要 spider.jar / 引擎 才能在手机上播放（即用户说的「爬虫类不能用的」）。

    真实信号不在 type 字段（上游把大量可用标准接口也标成了 type=1），
    而在 ext / api 是否指向 spider 或引擎脚本。命中即视为手机端不可用，拆分层直接丢弃。
    注意：标准 MacCMS 接口（ext 为空、api 为 api.php/provide/vod）不会被命中，
          故用户置顶的「❤闪电资源」(type=1, ext='') 会被保留。
    """
    ext = str(s.get("ext", "") or "").lower()
    api = str(s.get("api", "") or "").lower()
    blob = ext + " " + api
    if ext.startswith("jar:") or ext.startswith("py:") or ext.startswith("js:") or "js:" in blob:
        return True
    if "spider" in blob:
        return True
    if "csp_" in api:                      # 引擎标识
        return True
    if "engine" in blob or "drpy" in blob or "dr_py" in blob:
        return True
    if ext and (ext.endswith(".js") or "/js/" in ext or "github" in ext and ext.endswith(".js")):
        return True
    return False


def pre_filter(sites):
    """拆分前预处理：移除音频 / 用户指定删除 / 藤谐音 / 需插件的爬虫·引擎源，返回 (保留列表, 计数)。"""
    rm = {"audio": 0, "user_remove": 0, "teng": 0, "plugin": 0}
    kept = []
    for s in sites:
        nm = str(s.get("name", "") or "")
        if is_user_remove(nm):
            rm["user_remove"] += 1
            continue
        if has_teng(nm, s.get("key", "")):
            rm["teng"] += 1
            continue
        if is_audio(nm):
            rm["audio"] += 1
            continue
        if is_plugin_source(s):          # 手机端需 spider.jar/引擎，直接丢弃
            rm["plugin"] += 1
            continue
        kept.append(s)
    return kept, rm

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
YSC_URL = os.environ.get(
    "YSC_JSON",
    "https://raw.githubusercontent.com/dearloyal/YSC/main/网站接口数据.json")
# 合并管线场景：上游产物已在同一工作目录，直接读本地（可用 LOCAL_YSC 覆盖）
LOCAL_YSC = os.environ.get("LOCAL_YSC", "网站接口数据.json")

# 用户指定从「全部」中剔除的名称（模糊匹配）
BLOCK_NAME_FRAGMENTS = ("酷我",)

# 用户指定提级白名单（2026-10-13）：命中 name/key 子串的 csp_ 引擎源跳过 ①.5 结构性预分流，
# 提级白名单（方案A，2026-10-13）：短剧/哔哩等 csp_ 引擎外部不可验证，且必须由完整单仓自带的
# spider.jar 提供引擎实现才能运行；全部.json 顶层无 spider，拆进来只会得到"有名字、没代码"的
# 空壳（看不了，且把主订阅从 620 污染到 1994）。故清空本元组——这类引擎源全部退回候选层，改由
# 用户在 App 里单独导入 fish2035 完整配置（其自带 spider，见 ysc_build.EXTRA_SOURCES）。probe.py
# 的 `if PROMOTE_SUBSTR and ...` 守卫随之失效，白名单引擎源会被踢回候选层。
# 若将来全部.json 顶层补了 spider 且确要提级，再把子串加回本元组。
PROMOTE_SUBSTR = ()

# 置顶规则：全部.json 把含此关键字的源放最前；部分.json 同理
PIN_ALL_FIRST = "❤闪电资源"   # 全部.json 红线：首位固定闪电资源
# 注：部分.json 已取消「老色逼」置顶红线（用户 2026-10-12 要求，纯按有效分类排序）

# 标志前缀
FLAG_NORMAL = "影视 "
FLAG_ADULT = "18 "

# ── 候选层（2026-10-13 分层改造）──
# 结构性无法外部验证的引擎源（api 非 http：csp_/相对路径/jar:/js:/py:）在这里
# 单独成层，probe 阶段会把连续失败的失效源也追加进来。
CAND_FILE = "候选.json"
CAND_META = {
    "name": "候选源（未验证 / 失效）",
    "subset_meaning": "候选 = 结构上无法外部验证的引擎源，或连续探活失败的失效源；"
                      "导入后如不可用可单独删除，不影响验证层",
}

# ── 分类缓存（离线抓取生成，防止每日流水线把真实分类打回空壳）──
# categories_cache.json: { norm_api(url): [分类名, ...] }，与上游分类做并集合并，
#                       把上游配置裁剪掉的真实分类（如闪电资源的短剧分类）补回来。
# empty_cats_cache.json: { norm_api(url): [空分类名, ...] }，逐源验证为空的分类，运行时剔除。
# （用户 2026-10-12：闪电资源「电影/电视剧/综艺/动漫」等标准分类 App 里点开是空的；
#   苹果CMS provide 接口筛选参数是 t 而非 type，?ac=videolist&t=<id> 的 total 即真实内容量，
#   已用 probe_empty_cats.py 全量验证并生成 empty_cats_cache.json。）
def norm_api(api):
    a = (api or "").strip().lower()
    a = re.sub(r"^https?://", "", a)
    a = re.sub(r"\?.*$", "", a)   # 去掉查询串（如 ?ac=list），仅按地址归一，避免 ?ac=list 有无导致匹配失败
    a = a.rstrip("/")
    a = re.sub(r"^www\.", "", a)
    return a

def has_real_categories(site):
    c = site.get("categories")
    return isinstance(c, list) and any(isinstance(x, str) and x.strip() for x in c)

def displayable_categories(site):
    """分类能否在 App 显示：须有真实分类，且 type != 0（用户 2026-10-12 确认 type=0 不显示分类）。"""
    return has_real_categories(site) and site.get("type") != 0

def _cache_path(name):
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    return p if os.path.exists(p) else name

_CACHE_FILE = _cache_path("categories_cache.json")
_EMPTY_CACHE_FILE = _cache_path("empty_cats_cache.json")

def _load_json_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def merge_categories_cache(sites):
    """用 categories_cache.json 补回上游配置未提供的真实分类（并集合并）。

    2026-10-13 修复：原实现只在分类列表为空时才整体补，导致上游配置裁剪分类时
    缓存永远加不回来——当日流水线把「❤闪电资源」分类从 53 砍到 32，丢失全部
    短剧分类（爽文短剧/擦边短剧/漫剧），用户反馈「看不了短剧」。
    现改为并集：以离线抓取的真实分类为准，把上游缺失的分类按原顺序追加到列表
    尾部（不改动上游已有分类的顺序）；随后 remove_empty_cats 会剥掉验证为空的
    父级分类（电影/电视剧/综艺/动漫 等），二者配合保证「真实分类不丢、空壳不复活」。
    """
    cache = _load_json_cache(_CACHE_FILE)
    if not cache:
        return sites, 0
    out, filled = [], 0
    for s in sites:
        cats = cache.get(norm_api(s.get("api", "")))
        if cats:
            existing = s.get("categories")
            existing = [c for c in existing if isinstance(c, str)] if isinstance(existing, list) else []
            seen = {c.strip() for c in existing}
            add = [c for c in cats
                   if isinstance(c, str) and c.strip() and c.strip() not in seen]
            if add:
                s = dict(s)
                s["categories"] = existing + add
                filled += 1
        out.append(s)
    return out, filled

def remove_empty_cats(sites):
    """剔除 empty_cats_cache.json 中记录的「验证为空」分类（防每日流水线复活空壳分类）。"""
    cache = _load_json_cache(_EMPTY_CACHE_FILE)
    if not cache:
        return sites, 0
    out, cleaned = [], 0
    for s in sites:
        rm = cache.get(norm_api(s.get("api", "")))
        if rm and isinstance(s.get("categories"), list):
            rm_set = {str(x).strip() for x in rm}
            new_cats = [c for c in s["categories"] if not (isinstance(c, str) and c.strip() in rm_set)]
            if len(new_cats) != len(s["categories"]):
                s = dict(s)
                s["categories"] = new_cats
                cleaned += 1
        out.append(s)
    return out, cleaned

# 与 build 阶段共用同一份成人判定规则（adult_filter.py）
try:
    from adult_filter import is_adult as _is_adult
except Exception:
    _is_adult = None


def fetch_json(url, timeout=40):
    from urllib.parse import quote
    url = quote(url, safe=":/")  # 中文文件名需百分号编码（GitHub raw）
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
            capture_output=True, text=True, timeout=timeout + 15)
        return json.loads(r.stdout)
    except Exception as e:
        print("拉取 YSC 网站接口数据失败:", e)
        sys.exit(1)


def load_input():
    """优先读同仓库本地文件（合并管线），否则回退 raw 拉取（多仓链路）。"""
    if os.path.exists(LOCAL_YSC):
        with open(LOCAL_YSC, encoding="utf-8") as f:
            print(f"[QWJS] 读取本地文件: {LOCAL_YSC}")
            return json.load(f)
    print(f"[QWJS] 本地无 {LOCAL_YSC}，回退拉取: {YSC_URL}")
    return fetch_json(YSC_URL)


def adult_of(site):
    if _is_adult is not None:
        return _is_adult(site)
    # 兜底（极少触发）：直接导入失败时退回最简判定
    blob = " ".join(str(site.get(k, "")) for k in ("name", "key", "api"))
    return bool(re.search(r"(成人|🔞|色|黄|麻豆|av|avb|porn|hentai|萝莉)", blob, re.I))


def block(site):
    """是否命中剔除名单（酷我 等）。"""
    name = str(site.get("name", "") or "")
    return any(frag in name for frag in BLOCK_NAME_FRAGMENTS)


def apply_flag(site, is_adult):
    """给源名打标志前缀；已带前缀则幂等不加。"""
    name = str(site.get("name", "") or "")
    prefix = FLAG_ADULT if is_adult else FLAG_NORMAL
    if not name.startswith(prefix):
        site = dict(site)  # 不污染上游，写副本
        site["name"] = prefix + name
    return site


def pin_first(lst, keyword):
    """把名字含 keyword 的源整体置顶（其余保持原顺序）。"""
    head = [s for s in lst if keyword in str(s.get("name", ""))]
    tail = [s for s in lst if keyword not in str(s.get("name", ""))]
    return head + tail


def ensure_flags(sites):
    """统一补齐 filterable/searchable/quickSearch=1。
    否则影视仓对 filterable=0 的源不显示分类筛选栏（即使该源带 categories 也不显示）。"""
    out = []
    for s in sites:
        s = dict(s)
        s["filterable"] = 1
        s["searchable"] = 1
        s["quickSearch"] = 1
        out.append(s)
    return out


def main():
    d = load_input()
    sites = d.get("sites", [])
    print(f"[QWJS] 读取上游接口数: {len(sites)}")

    # 0) 合并分类缓存（离线抓取的真实分类），并集补回上游缺失的分类
    sites, filled = merge_categories_cache(sites)
    if filled:
        print(f"  从分类缓存补回真实分类: {filled} 条")

    # 0.2) 剔除逐源验证为空的分类（empty_cats_cache.json，防空壳分类复活）
    sites, cleaned = remove_empty_cats(sites)
    if cleaned:
        print(f"  剔除验证为空的分类: {cleaned} 个源")

    # 0.5) 统一补齐 filterable/searchable/quickSearch=1（否则 App 不显示分类筛选栏）
    sites = ensure_flags(sites)

    # 0.6) 预处理过滤（Phase 0 策略外置，蓝图 1.5 位置）：音频 / 用户删除 / 藤谐音，必须在成人分流前
    sites, frem = pre_filter(sites)
    if any(frem.values()):
        print(f"  预处理过滤：音频 {frem['audio']} | 用户删除 {frem['user_remove']} | 藤谐音 {frem['teng']}")

    # ① 剔除酷我 等
    kept = [s for s in sites if not block(s)]
    removed = len(sites) - len(kept)
    if removed:
        print(f"  剔除（酷我 等）: {removed}")

    # ①.5) 结构性预分流：非 http 接口（csp_/相对路径/jar:/js:/py:）外部无法验证，
    #       移入候选层（候选.json）。http 的 .js 脚本仍留验证层（probe 验存在性）。
    #       白名单 PROMOTE_SUBSTR 命中的提级源例外：留验证层（用户要求可直接观看短剧/哔哩）。
    def is_structural(s):
        return not str(s.get("api", "") or "").lower().startswith("http")

    def is_promoted(s):
        blob = str(s.get("name", "") or "") + " " + str(s.get("key", "") or "")
        return any(sub in blob for sub in PROMOTE_SUBSTR)

    cand_structural = [s for s in kept if is_structural(s) and not is_promoted(s)]
    kept = [s for s in kept if (not is_structural(s)) or is_promoted(s)]
    if cand_structural:
        print(f"  结构性引擎源移入候选层: {len(cand_structural)}")

    # ② 按成人/正常分流（直连 + 爬虫 合并，不再区分）
    normal = [s for s in kept if not adult_of(s)]
    adult = [s for s in kept if adult_of(s)]
    cand_normal = [s for s in cand_structural if not adult_of(s)]
    cand_adult = [s for s in cand_structural if adult_of(s)]
    print(f"  全部（正常） {len(normal)} | 部分（成人） {len(adult)}"
          f" | 引擎源（已丢弃） {len(cand_normal) + len(cand_adult)}")

    # ③ 打标志
    normal = [apply_flag(s, False) for s in normal]
    adult = [apply_flag(s, True) for s in adult]
    cand_normal = [apply_flag(s, False) for s in cand_normal]
    cand_adult = [apply_flag(s, True) for s in cand_adult]

    # ④ 置顶规则（仅全部.json 保留红线；部分.json 已取消置顶，见下）
    normal = pin_first(normal, PIN_ALL_FIRST)

    # ⑤ 分类【能显示】的源排前面、其余排后面
    #    - 「能显示」= 有真实分类 且 type != 0（用户确认 type=0 源不显示分类筛选栏）；
    #    - 全部.json：保留红线(❤闪电资源)首位；部分.json：纯按此规则排。
    def sort_by_real_cat(lst, keep_first):
        if not lst:
            return lst
        if keep_first:
            top, rest = lst[0], lst[1:]
            return [top] + sorted(rest, key=lambda s: 0 if displayable_categories(s) else 1)
        return sorted(lst, key=lambda s: 0 if displayable_categories(s) else 1)

    normal = sort_by_real_cat(normal, keep_first=True)
    adult = sort_by_real_cat(adult, keep_first=False)
    # 候选层：正常引擎源按「有可显示分类」排前，成人引擎源排尾
    for s in cand_normal + cand_adult:
        s["_cand_reason"] = "engine"   # 供 probe 阶段统计与分层管理
    cand_all = (sort_by_real_cat(cand_normal, keep_first=False)
                + sort_by_real_cat(cand_adult, keep_first=False))

    def write(fname, subset, lst):
        # 写盘前去重：同一 (归一化 api + key) 视为同一源，只保留首次出现，
        # 消除上游多份配置重复收录导致的真重复（审计闸门据此拦截）。
        seen, uniq = set(), []
        for s in lst:
            k = (norm_api(s.get("api", "")), str(s.get("key", "") or "").strip())
            if k in seen:
                continue
            seen.add(k)
            uniq.append(s)
        if len(uniq) != len(lst):
            print(f"  去重：{subset} 移除 {len(lst) - len(uniq)} 个真重复")
        lst = uniq

        # 红线①：对外显示名严格只用「全部 / 部分 / 候选」，
        # 绝不出现"正常/成人/未验证/失效"等字样，也不外泄 subset_meaning/adult 等内部语义。
        meta = {
            "name": subset,                   # 全部 / 部分 / 候选
            "subset": subset,
            "source": "dearloyal/YSC",
            "count": len(lst),
        }
        if subset == "候选":
            # probe 阶段会在此基础上追加失效源并重写 _cand 统计
            meta["_cand"] = {
                "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "desc": "候选层源，导入后如不可用可单独删除，不影响验证层",
                "from_classify": len(lst),
                "demoted_now": 0,
                "engine": len(lst),
                "revived_dropped": 0,
            }
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({**meta, "sites": lst}, f, ensure_ascii=False, indent=2)
        print(f"  写出 {fname} ({len(lst)})")

    write("全部.json", "全部", normal)
    write("部分.json", "部分", adult)
    print("[QWJS] 完成（候选层已废弃，引擎源不再输出）")


if __name__ == "__main__":
    main()
