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
import json, os, re, sys, time, subprocess

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
YSC_URL = os.environ.get(
    "YSC_JSON",
    "https://raw.githubusercontent.com/dearloyal/YSC/main/网站接口数据.json")
# 合并管线场景：上游产物已在同一工作目录，直接读本地（可用 LOCAL_YSC 覆盖）
LOCAL_YSC = os.environ.get("LOCAL_YSC", "网站接口数据.json")

# 用户指定从「全部」中剔除的名称（模糊匹配）
BLOCK_NAME_FRAGMENTS = ("酷我",)

# 置顶规则：全部.json 把含此关键字的源放最前；部分.json 同理
PIN_ALL_FIRST = "❤闪电资源"   # 全部.json 红线：首位固定闪电资源
# 注：部分.json 已取消「老色逼」置顶红线（用户 2026-10-12 要求，纯按有效分类排序）

# 标志前缀
FLAG_NORMAL = "影视 "
FLAG_ADULT = "18 "

# ── 分类缓存（离线抓取生成，防止每日流水线把真实分类打回空壳）──
# categories_cache.json: { norm_api(url): [分类名, ...] }，仅用于填补上游配置缺失的分类。
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
    """用 categories_cache.json 补回上游配置未提供的真实分类；不覆盖已有真实分类。"""
    cache = _load_json_cache(_CACHE_FILE)
    if not cache:
        return sites, 0
    out, filled = [], 0
    for s in sites:
        if has_real_categories(s):
            out.append(s)
            continue
        cats = cache.get(norm_api(s.get("api", "")))
        if cats:
            s = dict(s)
            s["categories"] = cats
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

    # 0) 合并分类缓存（离线抓取的真实分类），填补上游配置缺失项
    sites, filled = merge_categories_cache(sites)
    if filled:
        print(f"  从分类缓存补回真实分类: {filled} 条")

    # 0.2) 剔除逐源验证为空的分类（empty_cats_cache.json，防空壳分类复活）
    sites, cleaned = remove_empty_cats(sites)
    if cleaned:
        print(f"  剔除验证为空的分类: {cleaned} 个源")

    # 0.5) 统一补齐 filterable/searchable/quickSearch=1（否则 App 不显示分类筛选栏）
    sites = ensure_flags(sites)

    # ① 剔除酷我 等
    kept = [s for s in sites if not block(s)]
    removed = len(sites) - len(kept)
    if removed:
        print(f"  剔除（酷我 等）: {removed}")

    # ② 按成人/正常分流（直连 + 爬虫 合并，不再区分）
    normal = [s for s in kept if not adult_of(s)]
    adult = [s for s in kept if adult_of(s)]
    print(f"  全部（正常） {len(normal)} | 部分（成人） {len(adult)}")

    # ③ 打标志
    normal = [apply_flag(s, False) for s in normal]
    adult = [apply_flag(s, True) for s in adult]

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

    def write(fname, subset, lst):
        meta = {
            "name": f"{'全部' if subset == '全部' else '部分'}接口（{'正常' if subset == '全部' else '成人'}）",
            "subset": subset,                # 全部 / 部分
            "subset_meaning": "全部 = 正常可用接口" if subset == "全部" else "部分 = 成人接口",
            "adult": subset == "部分",
            "source": "dearloyal/YSC",
            "count": len(lst),
        }
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({**meta, "sites": lst}, f, ensure_ascii=False, indent=2)
        print(f"  写出 {fname} ({len(lst)})")

    write("全部.json", "全部", normal)
    write("部分.json", "部分", adult)
    print("[QWJS] 完成")


if __name__ == "__main__":
    main()
