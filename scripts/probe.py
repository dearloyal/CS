#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口探活（阶段4 · 严格模式）
============================
必须对接口真正发起请求、且**返回了有效影视数据**才算通过；判定失败的源直接删除。

用户选定的策略（2026-10-10 改版）：
  · 判定标准：不只要连得上，必须能拿到数据（有列表/点播内容）
  · 失败处理：**删除**（此前是保留并加 [失效] 后缀）

覆盖范围：
  api 是 http(s) 地址的站点才能验证。`csp_Xxx` 等引擎 Spider 没有 HTTP 地址，
  无从验证，一律**保留**（不删），并在统计里单列，不混入存活率。

验证方式：
  对接口按「标准苹果CMS参数」依次尝试（接口自带参数则直接测）：
      ?ac=videolist&pg=1  →  ?ac=list&pg=1  →  原始地址
  任一能返回有效数据即通过。全部失败后再整体复测 RETRY_ON_FAIL 轮，
  仍失败才判定为死源（防网络抖动误删）。
"""
import json, os, re, subprocess, time, datetime, argparse
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
FILES = [
    "1直连-全部.json", "1直连-部分.json",
    "2爬虫-全部.json", "2爬虫-部分.json",
    "接口合集.json",
]
CONCURRENCY = 24       # 并发验证数（严格模式请求更重，略降）
TIMEOUT = 8            # 单请求超时（秒）
RETRY_ON_FAIL = 1      # 全部尝试失败后的整体复测轮数
MAX_BODY = 300_000     # 响应体截断上限（字节），防止超大响应拖慢

DATA_KEYS = ("list", "data", "videos", "video", "class", "items")
DATA_MARK = re.compile(r"<video>|vod_play_url|vod_name|<list>|\"vod_id\"|vod_pic")


def probe_key(s):
    """去重键：http 类按地址，引擎类按 (key, api)。"""
    api = str(s.get("api", "") or "")
    if api.lower().startswith("http"):
        return ("U", api)
    return ("E", s.get("key", ""), api)


def candidates(url):
    """待尝试的 URL 列表。已带参数的接口直接测，否则补标准 CMS 参数。"""
    if "?" in url:
        return [url]
    base = url.rstrip("/")
    return [
        f"{base}?ac=videolist&pg=1",   # 苹果CMS 新版
        f"{base}?ac=list&pg=1",        # 苹果CMS 兼容写法
        base,                          # 原始地址兜底
    ]


def fetch(url):
    """取回响应，返回 (code, ms, body)。失败返回 (0, 0, "")）。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "-m", str(TIMEOUT), "-A", UA,
             "-w", "\n__META__ %{http_code} %{time_total}", url],
            capture_output=True, text=True, timeout=TIMEOUT + 6)
        out = r.stdout or ""
        idx = out.rfind("__META__")
        if idx == -1:
            return 0, 0, ""
        meta = out[idx:].split()
        if len(meta) < 3:
            return 0, 0, ""
        body = out[:idx]
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY]
        return int(meta[1]), int(float(meta[2]) * 1000), body
    except Exception:
        return 0, 0, ""


def has_payload(text):
    """响应里是否真的含有影视数据。"""
    if not text or len(text.strip()) < 8:
        return False
    s = text.strip()
    # 结构化 JSON：存在非空的列表字段即算有数据
    if s[0] in "{[":
        try:
            d = json.loads(s)
        except Exception:
            d = None
        if isinstance(d, dict):
            for k in DATA_KEYS:
                v = d.get(k)
                if isinstance(v, list) and v:
                    return True
                if isinstance(v, dict) and v:
                    return True
        elif isinstance(d, list) and d:
            return True
    # XML 或其它文本：含典型CMS数据标记
    return bool(DATA_MARK.search(s))


def probe_strict(url):
    """严格验证：必须返回有效数据才算通过。返回 (alive, code, ms)。"""
    code, ms = 0, 0
    for attempt in range(RETRY_ON_FAIL + 1):
        for u in candidates(url):
            c, m, body = fetch(u)
            code, ms = c, m
            if has_payload(body):
                return True, c, m
        if attempt < RETRY_ON_FAIL:
            time.sleep(0.8)
    return False, code, ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="只验证前 N 个接口（本地调试用，默认全量）")
    ap.add_argument("--keep-dead", action="store_true",
                    help="保留死源并加 [失效] 后缀（旧行为，默认删除）")
    args = ap.parse_args()

    t0 = time.time()
    docs = {}
    for f in FILES:
        if not os.path.exists(f):
            print(f"[probe] 缺少 {f}，跳过", flush=True)
            continue
        with open(f, encoding="utf-8") as fp:
            docs[f] = json.load(fp)

    # 1) 收集待验证接口（跨文件按 key 去重，同一接口只验一次）
    seen, tasks = set(), []
    for d in docs.values():
        for s in d.get("sites", []):
            k = probe_key(s)
            if k[0] == "U" and k not in seen:
                seen.add(k)
                tasks.append(k[1])
    if args.sample:
        tasks = tasks[:args.sample]
    print(f"[probe] 严格模式：待验证接口 {len(tasks)} 个（并发 {CONCURRENCY}，"
          f"剔除未返回数据的源）", flush=True)

    # 2) 并发验证
    result = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            for url, (alive, code, ms) in zip(tasks, ex.map(probe_strict, tasks)):
                result[url] = (alive, code, ms)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 3) 判定写回：删除死源（或按开关保留）
    mode = "keep" if args.keep_dead else "delete"
    for f, d in docs.items():
        n_alive = n_dead = n_engine = n_skip = 0
        kept = []
        for s in d.get("sites", []):
            k = probe_key(s)
            s["_probe_at"] = ts
            if k[0] == "U" and k[1] in result:
                alive, code, ms = result[k[1]]
                s["_alive"] = bool(alive)
                s["_code"] = code
                s["_ms"] = ms
                if alive:
                    n_alive += 1
                    kept.append(s)
                else:
                    n_dead += 1
                    if mode == "keep":
                        name = str(s.get("name", "") or "")
                        if not name.endswith("[失效]"):
                            s["name"] = f"{name} [失效]"
                        kept.append(s)
                    # 删除模式：不 append，等于剔除
            elif k[0] == "U":
                # 本次未纳入验证：不作判定，保留（避免误删）
                s["_alive"] = None
                n_skip += 1
                kept.append(s)
            else:
                # 引擎类 Spider，无从验证，保留
                s["_alive"] = None
                n_engine += 1
                kept.append(s)
        before = len(d.get("sites", []))
        d["sites"] = kept
        total = n_alive + n_dead
        summary = {
            "at": ts,
            "standard": "必须返回有效影视数据（内容校验）",
            "mode": "删除失败源" if mode == "delete" else "保留并标记",
            "verified": total,
            "alive": n_alive,
            "dead": n_dead,
            "engine_kept": n_engine,
            "not_checked": n_skip,
            "alive_rate": f"{(n_alive / total * 100):.1f}%" if total else "-",
            "sites_before": before,
            "sites_after": len(kept),
        }
        d["_probe"] = summary
        # count 是 classify 阶段写入的「探活前」数量，删源后必须同步，
        # 否则文件自称 791 条、实际只有 238 条，App 端读数会自相矛盾。
        if "count" in d:
            d["count"] = len(kept)
        with open(f, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        print(f"[probe] {f:16s} 验证 {total:5d}  通过 {n_alive:5d}  "
              f"删除 {n_dead:5d}  引擎保留 {n_engine:5d}  未判定 {n_skip:5d}  "
              f"通过率 {summary['alive_rate']}  "
              f"站点 {before}→{len(kept)}", flush=True)

    print(f"[probe] 完成，用时 {(time.time() - t0):.1f}s", flush=True)


if __name__ == "__main__":
    main()
