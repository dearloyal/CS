#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口探活（阶段4 · 分层模式）
============================
2026-10-13 改版：从「单层严删」改为「验证层 + 候选层」双输出。

背景（用户诉求：全网能找到的都要，不能死一片删一片）：
  · 旧逻辑把未验证引擎(csp_/相对路径)与死链一律删掉——620 个小而纯的代价是
    每天蒸发上千个「暂时不可用」的源，用户感知是「源越来越少」。
  · 新逻辑分两层：
      验证层（全部.json / 部分.json）：100% 确认可用的源（_alive=True），
        ＋ 探活防抖——单日失败的源暂留验证层（_alive=None），
        连续失败 ≥2 次才降级，防止网络抖动误杀。
      候选层（候选.json）：结构上无法外部验证的引擎源
        （classify 阶段预分流写入）＋ 连续失败 ≥2 次的失效源
        （本阶段追加，名字标 [失效]）。
  · 任一日探活成功即回验证层并清零计数（probe_history.json 持久化，
    由 probe job 的 checkout 从 main 读取昨日值）。

与上游的协作：
  · qwjs_classify.py 已把非 http 接口（csp_/相对路径/jar:/js:/py:）预分流到
    候选.json，本脚本对 全部.json/部分.json 里残留的引擎源同样转候选层（防御）。
  · 失效源名字统一加 " [失效]" 后缀，用户导入候选层后可自行甄别。

验证方式：
  对接口按「标准苹果CMS参数」依次尝试（接口自带参数则直接测）：
      ?ac=videolist&pg=1  →  ?ac=list&pg=1  →  原始地址
  任一能返回有效数据即通过。全部失败后再整体复测 RETRY_ON_FAIL 轮，
  仍失败才判定为死源（防网络抖动误删）。
"""
import json, os, re, subprocess, time, datetime, argparse
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

# 提级白名单（与 qwjs_classify.PROMOTE_SUBSTR 保持一致）：命中子串的引擎源在验证层保留，
# 不被 ① 踢回候选层。导入失败时降级为空元组（所有引擎源按原逻辑进候选层）。
try:
    from qwjs_classify import PROMOTE_SUBSTR
except Exception:
    PROMOTE_SUBSTR = ()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
FILES = [
    "全部.json", "部分.json",
]
CAND_FILE = "候选.json"            # 候选层输出（classify 预分流 + 本阶段追加失效源）
HISTORY_FILE = "probe_history.json"  # 连续失败计数（跨日防抖状态）
CONCURRENCY = 24       # 并发验证数（严格模式请求更重，略降）
TIMEOUT = 8            # 单请求超时（秒）
RETRY_ON_FAIL = 1      # 全部尝试失败后的整体复测轮数
MAX_BODY = 300_000     # 响应体截断上限（字节），防止超大响应拖慢
FAIL_DEMOTE = 2        # 连续失败多少次降级候选层（防抖阈值）

DATA_KEYS = ("list", "data", "videos", "video", "class", "items")
DATA_MARK = re.compile(r"<video>|vod_play_url|vod_name|<list>|\"vod_id\"|vod_pic")

# 引擎蜘蛛脚本：api 指向 .js/.py/.json 等文件（而非 CMS 接口）。
# 这类地址不能用「是否返回影视数据」判断，只能看文件还在不在（HTTP 状态码）。
SCRIPT_RE = re.compile(r"\.(js|py|min\.js|json)(\?|$|&)", re.I)
# 国内代理包裹的真实地址：https://gh-proxy.com/https://raw.githubusercontent.com/...
# 剥掉代理前缀才能从 CI 网络直达底层文件，避免把可达的脚本误判死。
PROXY_RE = re.compile(r"^https?://[^/]+/(https?://.+)$")

CAND_META = {
    "name": "候选",
    "subset": "候选",
    "source": "dearloyal/YSC",
}


def probe_key(s):
    """去重键：http 类按地址，引擎类按 (key, api)。"""
    api = str(s.get("api", "") or "")
    if api.lower().startswith("http"):
        return ("U", api)
    return ("E", s.get("key", ""), api)


def norm_api(api):
    """归一化接口地址：去 query、去尾部斜杠、host 转小写，用作去重键。

    例： https://x.com/a/?ac=list 与 https://x.com/a/ 视为同一后台。
    """
    if not api:
        return api
    api = api.strip()
    try:
        sp = urlsplit(api)
        host = sp.netloc.lower()
        path = sp.path.rstrip("/")
        return urlunsplit((sp.scheme.lower(), host, path, "", ""))
    except Exception:
        return api.lower()


def cand_key(s):
    """候选层去重键：U 类按归一化 api，E 类按 (key, api)。"""
    k = probe_key(s)
    if k[0] == "U":
        return ("U", norm_api(k[1]))
    return k


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


def probe_script(url):
    """引擎蜘蛛脚本的存在性校验。

    脚本文件不会返回影视数据，只能用 HTTP 状态码判断「文件是否还在」：
      · 200/2xx/3xx      → 文件在，存活（保留）
      · 404 / 410         → 文件已删/移走，死（删除）
      · 连接失败/超时/其它 → 不确定，保留不删（防 CI 网络抖动误删）
    同时剥掉 gh-proxy 等国内代理前缀，直达底层 raw 地址再查。
    返回 (alive, code, ms)。
    """
    real = PROXY_RE.sub(r"\1", url)
    code, ms = 0, 0
    for attempt in range(RETRY_ON_FAIL + 1):
        code, ms, _ = fetch(real)
        if code and 200 <= code < 400:
            return True, code, ms
        if code in (404, 410):
            return False, code, ms
        if attempt < RETRY_ON_FAIL:
            time.sleep(0.8)
    return True, code, ms


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="只验证前 N 个接口（本地调试用，默认全量）")
    ap.add_argument("--keep-dead", action="store_true",
                    help="死源当天即降级候选层（防抖阈值降为 1，旧行为）")
    ap.add_argument("--demote", type=int, default=FAIL_DEMOTE,
                    help=f"连续失败多少次降级候选层（默认 {FAIL_DEMOTE}）")
    args = ap.parse_args()
    demote_at = 1 if args.keep_dead else max(1, args.demote)

    t0 = time.time()
    docs = {}
    for f in FILES:
        if not os.path.exists(f):
            print(f"[probe] 缺少 {f}，跳过", flush=True)
            continue
        with open(f, encoding="utf-8") as fp:
            docs[f] = json.load(fp)

    # 0) 读昨日防抖状态 + 昨日候选层（classify 预分流的引擎源在里面）
    history = load_json(HISTORY_FILE, {})
    if not isinstance(history, dict):
        history = {}
    cand_doc = load_json(CAND_FILE, None)

    # 1) 收集待验证接口（跨文件按 key 去重，同一接口只验一次）
    seen, tasks = set(), []
    for d in docs.values():
        for s in d.get("sites", []):
            k = probe_key(s)
            if k[0] == "U" and k[1] not in seen:
                seen.add(k[1])
                kind = "script" if SCRIPT_RE.search(k[1]) else "cms"
                tasks.append((k[1], kind))
    if args.sample:
        tasks = tasks[:args.sample]
    n_script = sum(1 for _, kind in tasks if kind == "script")
    print(f"[probe] 分层模式：待验证接口 {len(tasks)} 个"
          f"（CMS {len(tasks) - n_script} / 引擎脚本 {n_script}），并发 {CONCURRENCY}，"
          f"连续失败 {demote_at} 次降级候选层", flush=True)

    # 2) 并发验证（CMS 验数据；引擎脚本验文件存在性）
    def verify(t):
        url, kind = t
        return probe_script(url) if kind == "script" else probe_strict(url)

    result = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            for t, (alive, code, ms) in zip(tasks, ex.map(verify, tasks)):
                result[t[0]] = (alive, code, ms)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 3) 分层判定写回
    #    验证层 = _alive=True（确认可用）+ _alive=None（防抖暂留/未测）
    #    候选层 = 引擎源（结构性不可验证）+ 连续失败 ≥ 阈值的失效源
    cand_sites = list((cand_doc or {}).get("sites", [])) if cand_doc else []
    n_from_classify = len(cand_sites)
    new_history = {}
    for f, d in docs.items():
        n_alive = n_dead = n_engine = n_skip = 0
        n_bounce = 0          # 防抖暂留（单日失败）
        n_demote = 0          # 本次降级候选层
        kept, demoted = [], []
        for s in d.get("sites", []):
            k = probe_key(s)
            s["_probe_at"] = ts
            if k[0] == "U" and k[1] in result:
                alive, code, ms = result[k[1]]
                s["_alive"] = bool(alive)
                s["_code"] = code
                s["_ms"] = ms
                na = norm_api(k[1])
                if alive:
                    n_alive += 1
                    new_history.pop(na, None)   # 复活：清零计数
                    kept.append(s)
                else:
                    n_dead += 1
                    fail = int(history.get(na, {}).get("fail", 0)) + 1
                    if fail >= demote_at:
                        # 连续失败达到阈值 → 降级候选层，标 [失效]
                        n_demote += 1
                        name = str(s.get("name", "") or "")
                        base_name = name.replace(" [失效]", "")
                        s["name"] = f"{base_name} [失效]"
                        s["_cand_reason"] = "dead"
                        demoted.append(s)
                        new_history[na] = {"fail": fail, "at": ts, "code": code}
                    else:
                        # 单日失败：防抖暂留验证层（不改名，_alive=None）
                        n_bounce += 1
                        s["_alive"] = None
                        kept.append(s)
                        new_history[na] = {"fail": fail, "at": ts, "code": code}
            elif k[0] == "U":
                # 本次未纳入验证：不作判定，保留（避免误删）
                s["_alive"] = None
                n_skip += 1
                kept.append(s)
                na = norm_api(k[1])
                if na in history:
                    new_history[na] = history[na]   # 未测到，旧状态原样结转
            else:
                # 引擎类 Spider（classify 未预分流掉的残留）：结构性不可验证 → 候选层
                # 白名单 PROMOTE_SUBSTR 命中的短剧/哔哩引擎：用户要求留验证层，防踢回候选
                _blob = str(s.get("name", "") or "") + " " + str(s.get("key", "") or "")
                if PROMOTE_SUBSTR and any(sub in _blob for sub in PROMOTE_SUBSTR):
                    s["_alive"] = None
                    n_skip += 1
                    kept.append(s)
                    continue
                s["_alive"] = None
                s["_cand_reason"] = "engine"
                n_engine += 1
                demoted.append(s)

        # 3.5) 验证层最终化：http 按归一化 api 去重；引擎源按 (key,api) 身份去重
        #      （同一 api 的多个 csp_ 引擎靠 key/ext 区分，不能按 api 并掉，否则只留 1 个）
        seen_norm, final = set(), []
        for s in kept:
            k = probe_key(s)
            na = norm_api(k[1]) if k[0] == "U" else k
            if na in seen_norm:
                continue
            seen_norm.add(na)
            final.append(s)
        dedup_removed = len(kept) - len(final)

        before = len(d.get("sites", []))
        d["sites"] = final
        total = n_alive + n_dead
        summary = {
            "at": ts,
            "standard": "必须返回有效影视数据（内容校验）",
            "mode": f"验证层(100%可用+防抖暂留) / 候选层(引擎+失效，连续失败>{demote_at - 1}次降级)",
            "verified": total,
            "alive": n_alive,
            "dead": n_dead,
            "bounced": n_bounce,
            "demoted": n_demote,
            "engine_to_cand": n_engine,
            "not_checked": n_skip,
            "dedup_removed": dedup_removed,
            "alive_rate": f"{(n_alive / total * 100):.1f}%" if total else "-",
            "sites_before": before,
            "sites_after": len(final),
        }
        d["_probe"] = summary
        # count 是 classify 阶段写入的「探活前」数量，删源后必须同步，
        # 否则文件自称 791 条、实际只有 238 条，App 端读数会自相矛盾。
        if "count" in d:
            d["count"] = len(final)
        with open(f, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        print(f"[probe] {f:16s} 验证 {total:5d}  通过 {n_alive:5d}  "
              f"死链 {n_dead:5d}（防抖留 {n_bounce:3d}/降级 {n_demote:3d}）  "
              f"引擎转候选 {n_engine:4d}  去重 {dedup_removed:5d}  "
              f"通过率 {summary['alive_rate']}  站点 {before}→{len(final)}", flush=True)
        # 收集本文件降级的源，稍后统一入候选层
        for s in demoted:
            cand_sites.append(s)

    # 4) 候选层最终化：与验证层去重（网站复活的源删旧 [失效] 条目）+ 层内去重
    verified_norm = set()
    for d in docs.values():
        for s in d.get("sites", []):
            verified_norm.add(norm_api(str(s.get("api") or "")))
    seen_c, final_c = set(), []
    n_revived = 0
    for s in cand_sites:
        na = norm_api(str(s.get("api") or ""))
        if na in verified_norm:
            n_revived += 1     # 验证层已有同名后台（复活或并行），旧候选条目作废
            continue
        ck = cand_key(s)
        if ck in seen_c:
            continue
        seen_c.add(ck)
        final_c.append(s)
    n_cand_engine = sum(1 for s in final_c if s.get("_cand_reason") == "engine")
    n_cand_dead = sum(1 for s in final_c if s.get("_cand_reason") == "dead")

    cand_out = dict(CAND_META)
    cand_out.update({
        "count": len(final_c),
        "sites": final_c,
        "_from": f"https://raw.githubusercontent.com/dearloyal/CS/main/{CAND_FILE}",
        "_cand": {
            "at": ts,
            "desc": "候选层源，导入后如不可用可单独删除，不影响验证层",
            "from_classify": n_from_classify,
            "demoted_now": n_cand_dead,
            "engine": n_cand_engine,
            "revived_dropped": n_revived,
        },
    })
    with open(CAND_FILE, "w", encoding="utf-8") as fp:
        json.dump(cand_out, fp, ensure_ascii=False, indent=2)
    print(f"[probe] 候选层 {len(final_c)} 个（classify 预分流 {n_from_classify} / "
          f"本次降级 {n_cand_dead} / 引擎 {n_cand_engine}，复活清理 {n_revived}）",
          flush=True)

    # 5) 持久化防抖状态（只留 fail>0 的条目；超过 14 天未刷新的清理，防无限增长）
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pruned = {k: v for k, v in new_history.items()
              if str(v.get("at", "")) >= cutoff}
    n_pruned = len(new_history) - len(pruned)
    with open(HISTORY_FILE, "w", encoding="utf-8") as fp:
        json.dump(pruned, fp, ensure_ascii=False, indent=2)
    print(f"[probe] 防抖状态 {len(pruned)} 个接口处于连续失败中"
          f"（清理超龄 {n_pruned} 个）", flush=True)

    print(f"[probe] 完成，用时 {(time.time() - t0):.1f}s", flush=True)


if __name__ == "__main__":
    main()
