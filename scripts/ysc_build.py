#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YSC 数据源构建脚本（部署到 dearloyal/YSC → scripts/build.py）
=============================================================
任务（按用户要求）：
  1. 拉取三个老源 LunaTV 配置（hafrey1 / hookybaby / netput-web / zyunling），
     统一转换成「影视仓可用」的 sites 格式（type=1 + 可搜索/筛选字段）；
  2. 拉取 0.cdz.qzz.io/list.txt —— 即饭太硬 + 0.cdz 网页里列出的【全部线路接口】
     （饭太硬 / 南风 / 王二小 / 肥猫 … 每条含主 URL 与多备份）；
  3. 上面每一条配置 URL 都经过「饭太硬解密端点 jiemi.php?url=<URL>」解密，
     转换为影视仓可用接口；
  4. 产出两个文件（均放在仓库根，供用户订阅 / 供 QWJS 读取）：
     - 网站接口数据.json : 全部经解密得到的扁平 sites 聚合（按地址去重：
                           真实接口/csp 按归一化地址合并重复，drpy/js/py 共享
                           引擎路径保留 key 区分），QWJS 每周一会读取它做分类；
     - 接口合集.json     : 可「手动选择线路」的合集 —— 内含每条线路元信息
                           （饭太硬 / 南风 / 王二小 …）＋ 合并的全部 sites，
                           用户导入影视仓后可手动切换线路。
运行：在仓库根目录执行  python3 scripts/build.py
"""
import json, os, re, sys, time, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEC = "http://www.xn--sss604efuw.net/jm/jiemi.php"
CDZ_LIST = "https://0.cdz.qzz.io/list.txt"

# 老三源（LunaTV 配置）
LAOSAN = [
    "https://raw.githubusercontent.com/hafrey1/LunaTV-config/main/jinhuang.json",
    "https://raw.githubusercontent.com/hafrey1/LunaTV-config/main/luna-tv-config.json",
    "https://raw.githubusercontent.com/hafrey1/LunaTV-config/main/LunaTV-config.json",
    "https://raw.githubusercontent.com/hookybaby/LunaTV-config/main/LunaTV-config.json",
    "https://raw.githubusercontent.com/netput-web/LunaTV-config/main/LunaTV-config.json",
    "https://raw.githubusercontent.com/zyunling/LunaTV-config/main/luna-tv-config.json",
]

# 不纳入的源（按 api 关键字屏蔽）
EXCLUDE_API = ("dinggetv", "wolongzyw.com", "pz.v88.qzz.io", "siwazyw.tv", "api.bwzyz.com")

# 额外手动指定源：不在 cdz list.txt 里、但你想纳入的独立源（例如独立的“老农民”线路）。
# 格式: (线路名, 配置URL)，留空则不影响现有逻辑。
EXTRA_SOURCES = [
    # ("老农民", "https://example.com/老农民.json"),
]

ADULT_KW = re.compile(
    r"(18|r18|成人|🔞|色|黄|麻豆|艾旦|sex|av|番号|福利|伦理|裸|春药|约炮|同志|同性|"
    r"avb|porn|hentai|jm|萝莉)", re.I)
CRAWLER_API = re.compile(r"^(csp_|drpy|spider|\./|jar:|js:|py:|http.*\.js$|http.*\.py$)", re.I)

# 地址归一化：去协议头、去末尾斜杠（用于真实接口地址去重）
def norm_api(api):
    a = api.strip().lower()
    a = re.sub(r"^https?://", "", a)
    a = a.rstrip("/")
    # 去掉末尾孤立的 “?”（如 .../provide/vod? 与 .../provide/vod 实为同一地址）
    a = re.sub(r"\?$", "", a)
    # 去掉 www. 前缀（如 lovedan.net 与 www.lovedan.net 实为同一站）
    a = re.sub(r"^www\.", "", a)
    return a

# 是否为“共享引擎”api（drpy/js/py 相对或远程引擎文件）：
# 这类 api 被很多不同源共用，源身份靠 key/ext 区分，不能按 api 地址去重
def _is_engine_api(api):
    l = api.lower()
    return l.endswith(".js") or l.endswith(".py") or l.startswith("./") or l.startswith("../")

# 去重键：引擎类保留 (key,api)；真实接口地址（http/csp 等）按归一化地址去重
def dedup_key(s):
    api = str(s.get("api", "")).strip()
    if _is_engine_api(api):
        return ("K", s.get("key", ""), api)
    return ("A", norm_api(api))


def run_curl(url, timeout=25):
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
            capture_output=True, text=True, timeout=timeout + 15)
        return r.stdout or ""
    except Exception:
        return ""


def load_laosan():
    """老三源 LunaTV(api_site 结构) → 影视仓 sites 格式。"""
    out = []
    for u in LAOSAN:
        txt = run_curl(u)
        try:
            d = json.loads(txt)
        except Exception:
            continue
        if "api_site" in d and isinstance(d["api_site"], dict):
            for k, v in d["api_site"].items():
                api = v.get("api", "")
                if not api or any(x in api for x in EXCLUDE_API):
                    continue
                out.append({
                    "key": k, "name": v.get("name", k), "api": api,
                    "type": 1, "searchable": 1, "quickSearch": 1, "filterable": 1,
                    "detail": v.get("detail", ""), "_line": v.get("name", k),
                    "_src": "老三源",
                })
        elif "sites" in d and isinstance(d["sites"], list):
            for s in d["sites"]:
                s = dict(s); s["_line"] = s.get("name", ""); s["_src"] = "老三源"
                out.append(s)
    return out


def load_cdz_lines():
    """0.cdz.qzz.io/list.txt → [(线路名, 配置URL), ...]（去重）。"""
    txt = run_curl(CDZ_LIST)
    rows = []
    seen = set()
    for line in txt.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        name = parts[0].replace(".json", "")
        url = parts[3]
        if not url.startswith("http"):
            continue
        if url in seen:
            continue
        seen.add(url)
        rows.append((name, url))
    return rows


# ── 宽松 JSON 解析 ────────────────────────────────────────────────
# jiemi.php 返回的是"伪 JSON"：带 // 注释头、BOM、尾逗号、块注释、
# 对象之后还有残留文本。旧版只做 first{ .. last} 切片 + 去整行 //，
# 会把大量本可解析的线路误判为 no-json（实测 84 条因此丢掉 5~13 条）。
# 这里改为：去注释 → 括号配对取第一个完整对象 → 尾逗号/控制字符兜底。
_TRAIL_COMMA = re.compile(r",(\s*[}\]])")


def _strip_comments(t, hashes=False):
    """去 BOM、整行 // 注释、块注释 /* */；hashes=True 时再去 # 注释行。
    不动 URL 里的 // 与 #；# 行只在前面都失败时才兜底去除，避免误删
    字符串内部以 # 开头的内容。"""
    t = t.replace("\ufeff", "").replace("﻿", "")
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    bad = ("//", "#") if hashes else ("//",)
    return "\n".join(l for l in t.splitlines()
                     if not l.strip().startswith(bad))


def _scan_object(t, start):
    """从 start 的 '{' 起做括号配对（跳过字符串与转义），返回匹配的 '}' 下标。"""
    depth = in_str = esc = 0
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = 0
            elif c == "\\":
                esc = 1
            elif c == '"':
                in_str = 0
            continue
        if c == '"':
            in_str = 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _objects(t, limit=3):
    """取 t 中前 limit 个括号配对的完整对象片段。"""
    out = []
    p = t.find("{")
    while p != -1 and len(out) < limit:
        e = _scan_object(t, p)
        if e == -1:
            break
        out.append(t[p:e + 1])
        p = t.find("{", p + 1)
    return out


def strip_json(text):
    """尽力从脏文本中取出第一个完整 JSON 对象，失败返回 None。
    候选顺序由保守到激进：原文 → 去 // → 去 //# → 各叠加去尾逗号。"""
    if not text:
        return None
    bases = [text]
    for h in (False, True):
        b = _strip_comments(text, h)
        if b not in bases:
            bases.append(b)
    for base in bases:
        for frag in _objects(base):
            cleaned = _TRAIL_COMMA.sub(r"\1", frag)
            for cand in (frag, cleaned):
                for strict in (True, False):
                    try:
                        return json.loads(cand, strict=strict)
                    except Exception:
                        pass
    return None


def decrypt(url, attempts=4):
    """经饭太硬 jiemi.php 解密一个配置 URL，返回 (dict, err)。"""
    last = ""
    for i in range(attempts):
        try:
            out = subprocess.run(
                ["curl", "-s", "-G", "--max-time", "50", "-A", UA, DEC,
                 "--data-urlencode", f"url={url}"],
                capture_output=True, text=True, timeout=65)
            text = out.stdout
        except Exception as e:
            last = f"exc:{e}"; time.sleep(1.5 + i); continue
        if not text or "无接口输入" in text:
            return None, "empty/无接口输入"
        if "解密失败" in text or "请检查" in text:
            # 端点明确报"URL 有误"，属源已失效，重试无意义
            return None, "解密失败/源已失效"
        data = strip_json(text)
        if not data:
            last = "no-json"; time.sleep(1.5 * (i + 1)); continue
        return data, ""
    return None, last


def tag(site):
    blob = " ".join(str(site.get(k, "")) for k in ("name", "key", "api", "type", "ext"))
    site["_adult"] = bool(ADULT_KW.search(blob))
    api = str(site.get("api", "")).strip()
    site["_crawler"] = bool(CRAWLER_API.match(api)) or (api.startswith("http") is False)
    return site


def main():
    t0 = time.time()
    # 1) 老三源
    lao = load_laosan()
    print(f"[1] 老三源 sites: {len(lao)}")

    # 2) cdz 网页全部线路 + 额外手动指定源
    lines = load_cdz_lines() + [(n, u) for n, u in EXTRA_SOURCES]
    print(f"[2] cdz 网页线路数: {len(lines)}（含额外 {len(EXTRA_SOURCES)} 条）")

    # 3) 逐条解密
    dec = []
    def work(item):
        name, url = item
        d, err = decrypt(url)
        return name, url, d, err
    with ThreadPoolExecutor(max_workers=4) as ex:
        for name, url, d, err in ex.map(work, lines):
            dec.append((name, url, d, err))
    ok_lines = sum(1 for _, _, d, _ in dec if d)
    print(f"[3] 解密完成: {ok_lines}/{len(lines)} 条成功, 用时 {(time.time()-t0):.1f}s")

    # 4) 聚合扁平 sites（去重）
    seen = set(); master = []; line_meta = []

    def add_site(s, line_name, src):
        if not isinstance(s, dict) or not s.get("api"):
            return
        key = dedup_key(s)
        if key in seen:
            return
        seen.add(key)
        s2 = tag(dict(s))
        s2["_line"] = line_name
        s2["_src"] = src
        master.append(s2)

    for s in lao:
        add_site(s, s.get("_line", "老三源"), "老三源")

    for name, url, d, err in dec:
        if d is None:
            line_meta.append({"name": name, "url": url, "status": "fail",
                              "error": err, "site_count": 0})
            continue
        sites = d.get("sites") if isinstance(d, dict) else None
        sc = len(sites) if isinstance(sites, list) else 0
        line_meta.append({"name": name, "url": url, "status": "ok",
                          "site_count": sc, "has_spider": "spider" in d,
                          "top_keys": list(d.keys())[:8]})
        if isinstance(sites, list):
            for s in sites:
                add_site(s, name, "网页线路")
        elif isinstance(d, dict) and d.get("api"):
            # 少数线路解密后是"单站点对象"而非容器（如 传说 / 肥猫），直接收录
            add_site(d, name, "网页线路")

    print(f"[4] 聚合去重 sites: {len(master)} | 直连 {sum(1 for s in master if not s['_crawler'])} "
          f"/ 爬虫 {sum(1 for s in master if s['_crawler'])} | 部分(成人) {sum(1 for s in master if s['_adult'])}")

    # 写 网站接口数据.json（数据层，QWJS 读取）
    w2 = {
        "name": "影视仓网站接口数据 (YSC · 经解密端点拉取)",
        "description": "老三源 + 0.cdz/饭太硬网页全部线路，逐条经饭太硬 jiemi.php 解密得到的扁平 sites 聚合。",
        "decrypt_endpoint": DEC,
        "decrypt_status": "ok",
        "source_laosan": len(lao),
        "source_lines": len(lines),
        "decoded_ok": ok_lines,
        "total": len(master),
        "sites": master,
    }
    with open("网站接口数据.json", "w", encoding="utf-8") as f:
        json.dump(w2, f, ensure_ascii=False, indent=2)
    print("    写出 网站接口数据.json")

    # 写 接口合集.json（可手动选择线路）
    w1 = {
        "name": "影视仓接口合集 (YSC)",
        "description": "可手动选择线路的接口合集：导入影视仓后，可在线路列表手动切换 饭太硬 / 南风 / 王二小 等。",
        "decrypt_endpoint": DEC,
        "lines": line_meta,
        "sites": master,
    }
    with open("接口合集.json", "w", encoding="utf-8") as f:
        json.dump(w1, f, ensure_ascii=False, indent=2)
    print("    写出 接口合集.json")

    print(f"[完成] 总用时 {(time.time()-t0):.1f}s")


if __name__ == "__main__":
    main()
