#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐源验证分类真实性，删除「验证为空」的分类（影视仓订阅源净化）
=================================================================
背景（用户 2026-10-12）：闪电资源里的「电影/电视剧/综艺/动漫」在 App 里
点开是空的。经接口验证：苹果CMS provide 接口的筛选参数是 **t**（不是 type）：
    ?ac=videolist&t=<type_id>  →  total 即该分类真实内容量
实测：闪电 t=1(电影) total=0（空），t=6(动作片) total=4977（真）；
      光速 t=1(电影) total=1（真，不能一刀切删名字）。

安全规则（防止误删）：
  1. 只处理「会显示分类」的源：type != 0 且有真实分类；
  2. 某分类仅当：成功解析到 type_id 且探测返回 total==0 → 判空；
  3. 仅当该源【至少 1 个分类探测到有内容】（证明 t 参数在该站生效）时，
     才删除其中判空的分类；否则（t 不可验证/站点异常）全部保留；
  4. 请求失败、解析失败的分类一律保留。
产出：
  - 清理后的 全部.json / 部分.json（原文件备份为 .bak.json）
  - empty_cats_cache.json：{ norm_api: [被删的空分类名] }，供每日流水线防复活

保守结转（2026-10-13 新增）：
  新缓存只记录「本次探测到空」的分类。旧缓存中本次未探测到的分类名
  （已不在任何源的当前分类列表里、或没匹配到 type_id 因而没被验证的，
  如闪电的 电影/电视剧/综艺/动漫）保留原判空结论，防止上游配置把
  这些空父级加回来时没人剥；本次探测过且未判空的名称，旧结论作废。
  旧缓存优先取当前工作目录的 empty_cats_cache.json，其次取脚本所在目录。
"""
import json, re, ssl, time, urllib.request, urllib.parse, sys, shutil, os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
PIN_ALL_FIRST = "❤闪电资源"
WORKERS = 24

def norm_api(api):
    a = (api or "").strip().lower()
    a = re.sub(r"^https?://", "", a); a = re.sub(r"\?.*$", "", a)
    a = a.rstrip("/"); a = re.sub(r"^www\.", "", a)
    return a

def _old_cache_path():
    """旧 empty_cats_cache.json：优先当前工作目录（输入文件所在），其次脚本目录。"""
    for p in (os.path.join(os.getcwd(), "empty_cats_cache.json"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "empty_cats_cache.json")):
        if os.path.exists(p):
            return p
    return None

def load_old_cache():
    p = _old_cache_path()
    if not p:
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def has_real(s):
    return isinstance(s.get("categories"), list) and any(isinstance(x, str) and x.strip() for x in s["categories"])

def displayable(s):
    return has_real(s) and s.get("type") != 0

def base_of(api):
    """去掉查询串，保留路径与斜杠。"""
    return (api or "").strip().split("?")[0].split("#")[0]

def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(600000)

def parse_class_map(raw):
    """解析 ?ac=list → [(type_id, type_name)]（保序去重，同名取首个）。"""
    text = raw.decode("utf-8", "ignore")
    pairs = []
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            cls = obj.get("class")
            if isinstance(cls, list):
                for c in cls:
                    if isinstance(c, dict):
                        tid = c.get("type_id"); nm = c.get("type_name") or c.get("name") or c.get("typename")
                        if isinstance(nm, str) and nm.strip():
                            pairs.append((str(tid).strip() if tid is not None else None, nm.strip()))
            if not pairs:
                lst = obj.get("list")
                if isinstance(lst, list):
                    for it in lst[:300]:
                        if isinstance(it, dict):
                            tid = it.get("type_id"); nm = it.get("type_name") or it.get("typename")
                            if isinstance(nm, str) and nm.strip():
                                pairs.append((str(tid).strip() if tid is not None else None, nm.strip()))
    except Exception:
        pass
    if not pairs and text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(text)
            # MacCMS XML：<list><video><tid>1</tid><name>电影</name></video></list>
            for v in root.iter():
                if v.tag.lower() == "video":
                    tid = None; nm = None
                    for ch in v:
                        t = ch.tag.lower()
                        if t == "tid" and ch.text and ch.text.strip(): tid = ch.text.strip()
                        elif t in ("name", "type_name", "typename") and ch.text and ch.text.strip(): nm = ch.text.strip()
                    if nm: pairs.append((tid, nm))
            # <class>/<types> 容器兜底
            if not pairs:
                for cls_el in root.iter():
                    if cls_el.tag.lower() in ("class", "types", "vod_class"):
                        for ch in cls_el:
                            nm = ch.text.strip() if ch.text else ""
                            if nm: pairs.append((ch.attrib.get("id") or ch.attrib.get("tid"), nm))
        except Exception:
            pass
    seen, out = set(), []
    for tid, nm in pairs:
        if nm in seen: continue
        seen.add(nm); out.append((tid, nm))
    return out

def probe_total(url, timeout=12):
    """探测分类内容量 → (ok, total)。ok=False 表示请求/解析失败（未知）。"""
    try:
        raw = fetch(url, timeout)
    except Exception:
        return (False, None)
    text = raw.decode("utf-8", "ignore")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            t = obj.get("total")
            if isinstance(t, (int, float)): return (True, int(t))
            lst = obj.get("list")
            if isinstance(lst, list): return (True, len(lst))
            return (True, 0)
    except Exception:
        pass
    if text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(text)
            for el in root.iter():
                if el.tag.lower() == "list":
                    rc = el.attrib.get("recordcount")
                    if rc is not None:
                        try: return (True, int(rc))
                        except Exception: pass
                    return (True, sum(1 for x in el.iter() if x.tag.lower() == "video"))
        except Exception:
            pass
    return (False, None)

def sort_displayable(lst, pin_kw=None):
    if pin_kw:
        head = [s for s in lst if pin_kw in str(s.get("name", ""))]
        tail = [s for s in lst if pin_kw not in str(s.get("name", ""))]
        lst = head + tail
        if len(lst) > 1:
            top, rest = lst[0], lst[1:]
            return [top] + sorted(rest, key=lambda s: 0 if displayable(s) else 1)
        return lst
    return sorted(lst, key=lambda s: 0 if displayable(s) else 1)

def main():
    files = ["全部.json", "部分.json"]
    data = {}
    for f in files:
        shutil.copy(f, f.replace(".json", ".bak.json"))
        data[f] = json.load(open(f, encoding="utf-8"))

    # 收集待处理源
    todo = []   # (file, idx, site)
    for f in files:
        for i, s in enumerate(data[f]["sites"]):
            if displayable(s) and s.get("api"):
                todo.append((f, i, s))
    print(f"[*] 待探测源: {len(todo)}", flush=True)

    # ── Phase 1: 抓 ?ac=list 得到 name→tid 映射（按 base api 去重）──
    base_apis = {}
    for _, _, s in todo:
        b = base_of(s["api"])
        if b: base_apis.setdefault(b, None)
    print(f"[*] 唯一接口: {len(base_apis)}，抓取 class 映射 ...", flush=True)

    def fetch_map(b):
        for u in (b + "?ac=list", b.rstrip("/") + "/?ac=list", b + "?ac=videolist"):
            try:
                m = parse_class_map(fetch(u))
                if m: return b, m
            except Exception:
                time.sleep(0.1)
        return b, None

    maps = {}
    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(fetch_map, b) for b in base_apis]
        for fu in as_completed(futs):
            b, m = fu.result(); maps[b] = m; done += 1
            if done % 40 == 0: print(f"    class {done}/{len(base_apis)}", flush=True)
    ok_maps = sum(1 for m in maps.values() if m)
    print(f"[*] class 映射成功: {ok_maps}/{len(base_apis)}", flush=True)

    # ── Phase 2: 汇总所有待探测 (base, tid)，去重后并行探测 ──
    need = {}   # (base, tid) -> None
    site_req = {}  # (file, idx) -> {name: tid}
    probed_names = {}  # norm_api -> set(本次实际探测过的分类名)，供保守结转
    for f, i, s in todo:
        b = base_of(s["api"]); m = maps.get(b) or []
        id_of = {}
        for tid, nm in m:
            if tid and nm not in id_of: id_of[nm] = tid
        req = {}
        for c in s["categories"]:
            if isinstance(c, str) and c.strip():
                tid = id_of.get(c.strip())
                if tid: req[c.strip()] = tid; need[(b, tid)] = None
        site_req[(f, i)] = req
        k = norm_api(s["api"])
        if req:
            probed_names.setdefault(k, set()).update(req.keys())
    print(f"[*] 待探测分类请求（去重后）: {len(need)}", flush=True)

    totals = {}
    def do_probe(k):
        b, tid = k
        return k, probe_total(b + "?ac=videolist&t=" + urllib.parse.quote(tid))

    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(do_probe, k) for k in need]
        for fu in as_completed(futs):
            k, (ok, t) = fu.result(); totals[k] = (ok, t); done += 1
            if done % 500 == 0: print(f"    probe {done}/{len(need)}", flush=True)
    print(f"[*] 探测完成", flush=True)

    # ── Phase 3: 逐源判定并清理 ──
    empty_cache = {}     # norm_api -> [removed names]
    removed_stat = {}    # file -> [ (name, [removed]) ]
    tiny_stat = []       # (site_name, cat, total) 有内容但极少（<5），仅报告
    for f, i, s in todo:
        b = base_of(s["api"]); req = site_req[(f, i)]
        if not req: continue
        probed = {}
        for c, tid in req.items():
            ok, t = totals.get((b, tid), (False, None))
            if ok: probed[c] = t
        nonempty = [c for c, t in probed.items() if t > 0]
        empty = [c for c, t in probed.items() if t == 0]
        for c, t in probed.items():
            if 0 < t < 5: tiny_stat.append((s.get("name"), c, t))
        if not nonempty:
            continue   # t 参数在该站不可验证 → 全部保留
        if not empty:
            continue
        rm = [c for c in s["categories"] if isinstance(c, str) and c.strip() in empty]
        if not rm: continue
        sites = data[f]["sites"]
        sites[i] = dict(s)
        sites[i]["categories"] = [c for c in s["categories"] if not (isinstance(c, str) and c.strip() in empty)]
        k = norm_api(s["api"])
        empty_cache.setdefault(k, [])
        for c in rm:
            if c not in empty_cache[k]: empty_cache[k].append(c)
        removed_stat.setdefault(f, []).append((s.get("name"), rm))

    # ── 保守结转：旧缓存中本次未探测到的分类名，保留原判空结论 ──
    old_cache = load_old_cache()
    carried, voided = 0, 0
    for k, names in old_cache.items():
        seen = probed_names.get(k, set())
        for n in names:
            n = str(n).strip()
            if not n:
                continue
            if n in seen:
                voided += 1   # 本次探测过但新缓存未判空 → 有内容，旧结论作废
                continue
            cur = empty_cache.setdefault(k, [])
            if n not in cur:
                cur.append(n)  # 本次未探测到 → 保守保留旧结论
                carried += 1

    # ── 重新排序 + 写出 ──
    for f in files:
        sites = data[f]["sites"]
        sites = sort_displayable(sites, pin_kw=(PIN_ALL_FIRST if data[f].get("subset") == "全部" else None))
        data[f]["sites"] = sites
        data[f]["count"] = len(sites)
        json.dump(data[f], open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[*] 写出 {f}: {len(sites)} 源", flush=True)

    json.dump(empty_cache, open("empty_cats_cache.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ── 报告 ──
    print("\n══════════ 清理报告 ══════════")
    for f in files:
        rs = removed_stat.get(f, [])
        print(f"\n【{f}】清理源数: {len(rs)}")
        for nm, rm in rs:
            print(f"  - {nm}: 删除 {rm}")
    print(f"\n共清理 {sum(len(v) for v in removed_stat.values())} 个源，"
          f"删除空分类 {sum(len(r) for v in removed_stat.values() for _, r in v)} 个")
    print(f"empty_cats_cache.json: {len(empty_cache)} 个接口"
          f"（保守结转：保留 {carried} 个本次未探测的名字，作废 {voided} 个实测有内容的旧结论）")
    if tiny_stat:
        print(f"\n⚠ 有内容但极少（1-4 条）的分类 {len(tiny_stat)} 个，暂保留：")
        for nm, c, t in tiny_stat[:40]:
            print(f"  - {nm} → {c} ({t} 条)")
        if len(tiny_stat) > 40: print(f"  ...等共 {len(tiny_stat)} 个")

if __name__ == "__main__":
    main()
