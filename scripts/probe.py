#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口探活（阶段4 · 健康标记）
============================
对本仓已落地的订阅文件做连通性探测，把结果写回每个站点对象。

用户选定的策略：
  · 判定标准：**只判能否连通**，不校验返回内容
  · 失败处理：**保留站点，只做标记，不剔除**

覆盖范围：
  只有 api 是 http(s) 地址的站点能被探测；`csp_Xxx` 这类引擎 Spider
  不含可访问地址，无法用 HTTP 探测，一律标记 unknown（不计入存活率）。

标记字段（写进每个 site）：
  _alive     true / false / null（null = 不可探测）
  _code      HTTP 状态码
  _ms        响应耗时（毫秒）
  _probe_at  探测时间（UTC）
「接口合集」额外给失活站点的 name 追加后缀，方便在 App 里肉眼分辨。
"""
import json, os, subprocess, time, datetime, argparse
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
FILES = [
    "1直连-全部.json", "1直连-部分.json",
    "2爬虫-全部.json", "2爬虫-部分.json",
    "接口合集.json",
]
DEAD_SUFFIX = "[失效]"
CONCURRENCY = 32       # 并发探测数
TIMEOUT = 6            # 单请求超时（秒）
RETRY_ON_FAIL = 1      # 判定失败后的复测次数：防网络抖动把好源误杀成死源


def probe_key(s):
    """去重键：http 类按地址，引擎类按 (key, api)。"""
    api = str(s.get("api", "") or "")
    if api.lower().startswith("http"):
        return ("U", api)
    return ("E", s.get("key", ""), api)


def once(url):
    """探测一次，返回 (code, ms, size)；彻底失败返回 (0, 0, 0)。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-L",
             "-w", "%{http_code} %{time_total} %{size_download}",
             "-m", str(TIMEOUT), "-A", UA, url],
            capture_output=True, text=True, timeout=TIMEOUT + 6)
        p = r.stdout.strip().split()
        if len(p) >= 3:
            return int(p[0]), int(float(p[1]) * 1000), int(float(p[2]))
    except Exception:
        pass
    return 0, 0, 0


def probe(url):
    """连通判定：2xx/3xx 且响应体非空算活；失败会复测 RETRY_ON_FAIL 次。"""
    code, ms = 0, 0
    for attempt in range(RETRY_ON_FAIL + 1):
        code, ms, size = once(url)
        if 200 <= code < 400 and size > 0:
            return True, code, ms
        if attempt < RETRY_ON_FAIL:
            time.sleep(0.6)
    return False, code, ms


def strip_dead_name(s):
    """站点恢复可用时把后缀摘掉（否则一旦标记就再也去不掉）。"""
    name = str(s.get("name", "") or "")
    if name.endswith(DEAD_SUFFIX):
        s["name"] = name[: -len(DEAD_SUFFIX)].rstrip()


def mark_dead_name(s):
    """给失活站点名字追加后缀（幂等，重复跑不会叠加）。"""
    name = str(s.get("name", "") or "")
    if name.endswith(DEAD_SUFFIX):
        return
    s["name"] = f"{name} {DEAD_SUFFIX}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="只探测前 N 个接口（本地调试用，默认全量）")
    args = ap.parse_args()

    t0 = time.time()
    docs = {}
    for f in FILES:
        if not os.path.exists(f):
            print(f"[probe] 缺少 {f}，跳过", flush=True)
            continue
        with open(f, encoding="utf-8") as fp:
            docs[f] = json.load(fp)

    # 1) 收集待探测接口（跨文件按 key 去重，同一接口只探一次）
    seen, tasks = set(), []
    for d in docs.values():
        for s in d.get("sites", []):
            k = probe_key(s)
            if k[0] == "U" and k not in seen:
                seen.add(k)
                tasks.append(k[1])
    if args.sample:
        tasks = tasks[:args.sample]
    print(f"[probe] 待探测接口 {len(tasks)} 个（并发 {CONCURRENCY}）", flush=True)

    # 2) 并发探测
    result = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            for url, (alive, code, ms) in zip(tasks, ex.map(probe, tasks)):
                result[url] = (alive, code, ms)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 3) 写回标记（保留全部站点）
    for f, d in docs.items():
        n_alive = n_dead = n_engine = n_skip = 0
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
                    if f == "接口合集.json":
                        strip_dead_name(s)
                else:
                    n_dead += 1
                    if f == "接口合集.json":
                        mark_dead_name(s)
            elif k[0] == "U":
                # 本次没纳入探测（如 --sample 截断）：不作任何判定，避免误杀
                s["_alive"] = None
                n_skip += 1
            else:
                s["_alive"] = None   # 引擎类，无从探测
                n_engine += 1
        total = n_alive + n_dead
        summary = {
            "at": ts,
            "standard": "连通性判定（不校验返回内容）",
            "probed": total,
            "alive": n_alive,
            "dead": n_dead,
            "engine_unprobeable": n_engine,
            "not_checked": n_skip,
            "alive_rate": f"{(n_alive / total * 100):.1f}%" if total else "-",
            "dead_suffix": DEAD_SUFFIX if f == "接口合集.json" else "",
        }
        d["_probe"] = summary
        with open(f, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        print(f"[probe] {f:16s} 探测 {total:5d}  活 {n_alive:5d}  死 {n_dead:5d}  "
              f"引擎类 {n_engine:5d}  未判定 {n_skip:5d}  存活率 {summary['alive_rate']}"
              + ("（已给死源加名字后缀）" if f == "接口合集.json" and n_dead else ""),
              flush=True)

    print(f"[probe] 完成，用时 {(time.time() - t0):.1f}s", flush=True)


if __name__ == "__main__":
    main()
