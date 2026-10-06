#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox / 影视仓 —— ceshi 终极兜底聚合器（杀手锏）
================================================
用途：当 yingshicang / quanwangjiansuo 都不可用时，这是最后一道保险。
     力求"最大覆盖 + 去重 + 分类"，每天自动刷新。

采集顺序（CS 只做"去重合并"，不做自主搜索；全网自主搜索由 QWJS 负责）：
  1. 先抓取 yingshicang 成品（YSC 已含上游老源 + 探测去重 + 分类）
  2. 再抓取 quanwangjiansuo 自搜结果（normal / adult 两份，直接并入）
  3. 全部按 api 地址去重合并
  4. 分类为 normal（不含成人）/ adult（纯成人）
  5. 保守探测：仅剔除"确定失效"(DNS失败/拒连/404/5xx)；超时一律保留；
     超时比例 > 30% 触发安全阀，保留全部。

输出：tvbox-normal.json / tvbox-adult.json（影视仓最小正确结构）。
"""
import sys, json, os, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (compatible; TVBoxAggregator/1.0)"
FETCH_TIMEOUT = 15
PROBE_TIMEOUT = 8
MAX_WORKERS = 16

# ── ① 先抓取 yingshicang 的成品（它已含上游老源 + 探测去重 + 分类）──
YINGSHICANG = [
    ("https://raw.githubusercontent.com/dearloyal/YSC/main/tvbox-normal.json", False),
    ("https://raw.githubusercontent.com/dearloyal/YSC/main/tvbox-adult.json", True),
]

# ── ② 再抓取 quanwangjiansuo 的自搜结果（直接并入）─────────────────
QUANWANGJIANSUO = [
    ("https://raw.githubusercontent.com/dearloyal/QWJS/main/tvbox-normal.json", False),
    ("https://raw.githubusercontent.com/dearloyal/QWJS/main/tvbox-adult.json", True),
]

ADULT_KEYWORDS = ["成人", "福利", "18禁", "🔞", "黄", "涩情", "萝莉", "裸",
                  "sex", "porn", "adult", "18+", "18禁"]
BAD_API_SUBSTR = ["dinggetv"]


def fetch(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def norm_api(api):
    a = (api or "").strip()
    if "?" in a:
        a = a.split("?", 1)[0]
    return a.rstrip("/")


# 只保留真正的影视仓标准接口，剔除明显无用的非标准源：
#   - 必须是 http(s) 真实地址（csp_ / spider 等自定义路由无配套脚本即空壳）
#   - 必须含标准苹果CMS接口 api.php/provide/vod
#   - 剔除内网/本地调试泄漏、以及把 GitHub 代理/raw 当 api 源的无效项
_GITHUB_PROXY_SUBSTR = ("githubusercontent.com", "ghproxy.com", "ghp.ci",
                        "ghfast.top", "jsdelivr.net", "raw.github",
                        "cdn.jsdelivr", "fastly.jsdelivr")


def is_usable_api(api):
    a = (api or "").strip().lower()
    if not a.startswith(("http://", "https://")):
        return False
    if "api.php/provide/vod" not in a:
        return False
    if any(x in a for x in ("127.0.0.1", "localhost", "0.0.0.0")):
        return False
    if any(x in a for x in _GITHUB_PROXY_SUBSTR):
        return False
    return True


def normalize_luna(obj):
    out = []
    api_site = obj.get("api_site") or {}
    for key, v in api_site.items():
        if isinstance(v, dict):
            api = v.get("api") or v.get("url")
            name = v.get("name") or key
            detail = v.get("detail") or ""
            is_adult = bool(v.get("is_adult", False))
        else:
            api, name, detail, is_adult = v, key, "", False
        if not api:
            continue
        out.append({"key": key, "name": name, "api": api,
                    "detail": detail, "is_adult": is_adult})
    return out


def normalize_tvbox(obj):
    out = []
    for s in obj.get("sites", []):
        if not isinstance(s, dict):
            continue
        api = s.get("api") or s.get("url")
        if not api:
            continue
        out.append({
            "key": s.get("key") or s.get("name") or api,
            "name": s.get("name") or s.get("key") or api,
            "api": api,
            "detail": s.get("detail") or s.get("url") or "",
            "is_adult": bool(s.get("is_adult", False)),
        })
    return out


def normalize_sitelist(obj):
    out = []
    for s in obj:
        if not isinstance(s, dict):
            continue
        api = s.get("api") or s.get("url")
        if not api:
            continue
        out.append({
            "key": s.get("key") or s.get("name") or api,
            "name": s.get("name") or s.get("key") or api,
            "api": api,
            "detail": s.get("detail") or s.get("url") or "",
            "is_adult": bool(s.get("is_adult", False)),
        })
    return out


def is_adult(name, flag):
    if flag:
        return True
    if name.startswith("🔞"):
        return True
    low = name.lower()
    for kw in ADULT_KEYWORDS:
        if kw.lower() in low:
            return True
    return False


def to_site(entry, used_keys):
    base = entry["key"] or norm_api(entry["api"])
    key = base
    i = 1
    while key in used_keys:
        key = f"{base}_{i}"
        i += 1
    used_keys.add(key)
    return {
        "key": key,
        "name": entry["name"],
        "api": entry["api"],
        "type": 1,
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 1,
        "detail": entry.get("detail", "") or "",
    }


def probe_api(api):
    sep = "&" if "?" in api else "?"
    url = api + sep + "ac=list&pg=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            code = r.status
        if code >= 400:
            return False, f"http{code}"
        return True, "ok"
    except urllib.error.HTTPError as e:
        if e.code >= 400:
            return False, f"http{e.code}"
        return True, "ok"
    except urllib.error.URLError as e:
        # 区分"确定失效"与"测不准"：DNS 解析失败/拒连=源已死；超时/重置=网络问题保留
        reason = str(getattr(e, "reason", e))
        if ("Name or service not known" in reason or "getaddrinfo" in reason
                or "nodename" in reason or "Connection refused" in reason
                or "No route" in reason):
            return False, "域名失效/拒连"
        return None, "超时/重置"
    except Exception as e:
        return None, str(e)[:40]


def do_probe(sites):
    apis = list({s["api"] for s in sites})
    status = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(probe_api, a): a for a in apis}
        for fu in as_completed(futs):
            a = futs[fu]
            st, msg = fu.result()
            status[a] = (st, msg)
    dead = {a for a, (st, _) in status.items() if st is False}
    unknown = {a for a, (st, _) in status.items() if st is None}
    ok = {a for a, (st, _) in status.items() if st is True}
    unk_ratio = len(unknown) / max(1, len(apis))
    # 安全阀：仅当"测不准(超时)比例过高"或"全部连不上"时，才怀疑 Runner 网络异常、保留全部；
    # 否则 DNS 失败/拒连/4xx/5xx 一律视为确定失效并剔除（这些是真死，不会被误删好源）。
    if unk_ratio > 0.30 or len(ok) == 0:
        print(f"[probe] ⚠ 超时比例 {unk_ratio:.2f} 或全不可达，疑似 Runner 网络异常，保留全部源")
        return sites
    kept = [s for s in sites if s["api"] not in dead]
    print(f"[probe] 确定失效 {len(dead)}/{len(apis)}（DNS/拒连/4xx/5xx），剔除 {len(sites) - len(kept)} 个")
    return kept


def add_entries(entries, seen, got, force_adult):
    added = 0
    filtered = 0
    for e in got:
        api = norm_api(e["api"])
        if not api:
            continue
        if any(b in api.lower() for b in BAD_API_SUBSTR):
            continue
        if not is_usable_api(api):
            filtered += 1
            continue
        if api in seen:
            continue
        seen.add(api)
        e["_adult"] = force_adult or is_adult(e["name"], e.get("is_adult", False))
        entries.append(e)
        added += 1
    return added, filtered


def load_and_normalize(url):
    """返回 (url, kind_or_None, force_adult, got_list, err)。"""
    try:
        raw = fetch(url)
        obj = json.loads(raw)
    except Exception as e:
        return url, None, False, None, e
    try:
        if isinstance(obj, dict) and "api_site" in obj:
            got = normalize_luna(obj)
        elif isinstance(obj, dict) and "sites" in obj:
            got = normalize_tvbox(obj)
        elif isinstance(obj, list):
            got = normalize_sitelist(obj)
        else:
            got = []
    except Exception as e:
        return url, None, False, None, e
    return url, "parsed", False, got, None


def main():
    entries = []
    seen = set()
    filtered_total = 0

    # ① yingshicang 的成品（已是影视仓 sites 结构，含上游老源 + 探测去重 + 分类）
    print("=== ① 抓取 yingshicang 成品（先于 quanwangjiansuo）===")
    for url, fa in YINGSHICANG:
        try:
            obj = json.loads(fetch(url))
            got = obj.get("sites", []) if isinstance(obj, dict) else []
        except Exception as e:
            print(f"[skip] {url}\n        -> {e}")
            continue
        n, f = add_entries(entries, seen, got, fa)
        filtered_total += f
        if n:
            print(f"[ok]   {url.split('/')[-1][:30]:<30} +{n}  (累计 {len(entries)})")

    # ② quanwangjiansuo 的自搜结果（已是影视仓 sites 结构）
    print("=== ② 抓取 quanwangjiansuo 自搜结果 ===")
    for url, fa in QUANWANGJIANSUO:
        try:
            obj = json.loads(fetch(url))
            got = obj.get("sites", []) if isinstance(obj, dict) else []
        except Exception as e:
            print(f"[skip] {url}\n        -> {e}")
            continue
        n, f = add_entries(entries, seen, got, fa)
        filtered_total += f
        if n:
            print(f"[ok]   {url.split('/')[-1][:30]:<30} +{n}  (累计 {len(entries)})")

    # ③ 分类（CS 只合并 YSC + QWJS，不再自己搜；全网自主搜索由 QWJS 负责）
    # 分类（不含成人 / 纯成人）
    used = set()
    normal, adult = [], []
    for e in entries:
        site = to_site(e, used)
        (adult if e["_adult"] else normal).append(site)

    # ④ 保守探测
    if os.environ.get("PROBE", "0") == "1":
        print("[probe] 开始保守探测...")
        normal = do_probe(normal)
        adult = do_probe(adult)

    # ⑤ 输出
    with open("tvbox-normal.json", "w", encoding="utf-8") as f:
        json.dump({"cache_time": 7200, "sites": normal}, f,
                  ensure_ascii=False, indent=2)
    with open("tvbox-adult.json", "w", encoding="utf-8") as f:
        json.dump({"cache_time": 7200, "sites": adult}, f,
                  ensure_ascii=False, indent=2)

    print(f"\n完成： 累计采集 {len(entries)} 唯一源 -> normal={len(normal)}  adult={len(adult)}")
    print(f"[filter] 本次剔除非标准/无效接口共 {filtered_total} 个")
    print("  normal:", "https://raw.githubusercontent.com/dearloyal/CS/main/tvbox-normal.json")
    print("  adult :", "https://raw.githubusercontent.com/dearloyal/CS/main/tvbox-adult.json")


if __name__ == "__main__":
    main()

