#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日"扫全网"发现器（自主发现，不依赖固定上游）
================================================
原理：GitHub 上有大量公开的 TVBox / LunaTV 配置 JSON，其中都包含
标准苹果CMS接口特征 "api.php/provide/vod"。用 GitHub 代码搜索
按【内容】而不是文件名搜索，就能每天自动发现新的配置文件——
今天有人新发布，明天就会被搜到并自动并入。

输入：--base 旧 discovered.txt（上一轮累积的发现，保证搜索偶发失败时不丢存量）
输出：合并后的 discovered.txt（每行一个 raw 地址），供 ysc_build.py 消费。

两条来源线：
  1) QUERIES          全网内容搜索，每天自动发现别人新发布的配置
  2) CURATED_UPSTREAM 人工精选的高产出聚合仓（已实测含大量可用站点、
     且多为每小时/每天自动更新），保证它们永远不被 MAX_RESULTS 裁掉
"""
import os, sys, json, argparse, time, urllib.parse, urllib.request, urllib.error

UA = "Mozilla/5.0 (compatible; TVBoxAggregator/1.0)"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
OWN_REPOS = ("dearloyal/quanwangjiansuo", "dearloyal/yingshicang", "dearloyal/ceshi")

# 内容特征搜索：不依赖任何固定仓库，谁发布都能搜到
QUERIES = [
    '"api.php/provide/vod" extension:json',
    '"api.php/provide/vod" tvbox extension:json',
    '"api.php/provide/vod" 影视 extension:json',
    'filename:luna-tv-config.json',
    'filename:LunaTV-config.json',
    '"api_site" "is_adult" extension:json',
]
PER_PAGE = 100          # 每次 API 请求最多返回 100 条
MAX_PAGES_PER_QUERY = 3 # 每个查询最多翻 3 页（GitHub 代码搜索上限 1000 条）
MAX_RESULTS = 400       # 最终送给聚合器的配置文件上限（按仓库 star 数排序取前 N）

# 精选上游：(仓库, 文件路径) —— 均为实测有产量且高频更新的公开聚合仓。
# 只登记仓库和路径，默认分支在运行时查询：上游从 main 切到 master 也不会 404。
CURATED_UPSTREAM = [
    ("25175/tvyuan", "tvbox_full.json"),       # 每小时更新，实测 1350 条可用
    ("25175/tvyuan", "tvbox.json"),
    ("qist/tvbox", "js.json"),                 # 1.1 万星，每天更新
    ("qist/tvbox", "dianshi.json"),
    ("qist/tvbox", "367.json"),
    ("Lightconer/tvbox-ysc-config", "output/单仓聚合.json"),  # 每天 2 次，饭太硬等 10 源聚合
    ("Lightconer/tvbox-ysc-config", "output/4k.json"),
    ("Lightconer/tvbox-ysc-config", "output/feimao.json"),
    ("Lightconer/tvbox-ysc-config", "output/ouge.json"),
    ("cluntop/tvbox", "box.json"),
    ("cluntop/tvbox", "fun.json"),
    ("cluntop/tvbox", "aa.json"),
    ("fswws/tvbox-source", "tvbox.json"),
    ("jiusanzhou/tvbox", "tvbox.json"),
]
# 给精选源一个极大的 star 值，保证排序后永远落在 MAX_RESULTS 之内
CURATED_STARS = 10 ** 9


def api_get(url, retries=3):
    """GET 一次 GitHub API；429/5xx 做指数退避重试。"""
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}" if TOKEN else "",
        })
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            last = e
            # 代码搜索限流很紧（429），退避后重试；4xx 中只有 429 值得重试
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (i + 1))
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def search(query):
    """返回 [(raw_url, stars)]，失败时抛异常由调用方兜底。"""
    out = []
    for page in range(1, MAX_PAGES_PER_QUERY + 1):
        q = urllib.parse.quote(query)
        url = (f"https://api.github.com/search/code?per_page={PER_PAGE}"
               f"&page={page}&q={q}")
        try:
            data = api_get(url)
        except Exception as e:
            print(f"[discover] 查询失败 {query!r} page{page}: {e}", file=sys.stderr)
            break
        time.sleep(2.5)  # 主动限速，避免触发 429
        items = data.get("items", [])
        if not items:
            break
        for it in items:
            repo = it.get("repository") or {}
            full = repo.get("full_name") or ""
            if not full or full.startswith("dearloyal/"):
                continue  # 跳过自己的仓库
            path = it.get("path") or ""
            if not path.endswith(".json"):
                continue
            branch = repo.get("default_branch") or "main"
            raw = f"https://raw.githubusercontent.com/{full}/{branch}/{path}".replace(" ", "%20")
            stars = (repo.get("stargazers_count") or 0)
            out.append((raw, stars))
        if len(items) < PER_PAGE:
            break
    return out


_branch_cache = {}


def default_branch(repo):
    """查询仓库默认分支。很多上游用的是 master 而非 main，写死会 404。"""
    if repo in _branch_cache:
        return _branch_cache[repo]
    br = ""
    try:
        d = api_get(f"https://api.github.com/repos/{repo}")
        br = d.get("default_branch") or ""
    except Exception as e:
        print(f"[discover] 查询 {repo} 默认分支失败: {e}", file=sys.stderr)
    _branch_cache[repo] = br or "main"
    return _branch_cache[repo]


def curated_urls():
    """精选上游的 raw 地址（按各仓库实际默认分支拼接，中文路径做 URL 编码）。"""
    out = []
    for repo, path in CURATED_UPSTREAM:
        br = default_branch(repo)
        p = "/".join(urllib.parse.quote(x) for x in path.split("/"))
        out.append(f"https://raw.githubusercontent.com/{repo}/{br}/{p}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="上一轮累积的 discovered.txt")
    args = ap.parse_args()

    found = {}  # raw -> stars
    # 1) 先载入存量（上一轮的发现），保证搜索失败时也不丢
    if args.base and os.path.exists(args.base):
        with open(args.base, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and line.startswith("http"):
                    found[line] = found.get(line, 0)
        print(f"[discover] 存量发现 {len(found)} 个", file=sys.stderr)

    # 2) 全网内容搜索
    total_new = 0
    for q in QUERIES:
        for raw, stars in search(q):
            if raw not in found:
                total_new += 1
            # star 数取较大值（同一文件可能被多次搜到）
            found[raw] = max(found.get(raw, 0), stars)
    print(f"[discover] 本次新发现 {total_new} 个，合计 {len(found)} 个", file=sys.stderr)

    # 2.5) 精选上游：必选，不参与 star 竞争
    for u in curated_urls():
        found[u] = max(found.get(u, 0), CURATED_STARS)
    print(f"[discover] 加入精选上游 {len(CURATED_UPSTREAM)} 个", file=sys.stderr)

    # 3) 按 star 数排序，取前 MAX_RESULTS 个（优先活跃维护的配置）
    ranked = sorted(found.items(), key=lambda kv: -kv[1])
    for raw, _ in ranked[:MAX_RESULTS]:
        print(raw)


if __name__ == "__main__":
    main()
