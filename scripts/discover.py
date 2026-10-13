#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日"扫全网"发现器 v2（自主发现，不依赖固定上游）
================================================
原理：TVBox / 影视仓 / FongMi 生态里，一个"配置文件"就是一个小型源库
（几十到上千个站点）。找到更多配置文件 = 找到更多网站。本脚本每天
自动三类发现，不依赖人工维护的固定上游：

  A. 代码内容搜索（QUERIES）：按内容特征搜 GitHub 上公开的配置 JSON
     （api.php/provide/vod、短剧、drpy、hiker、csp_ 等），谁发布都能搜到。
  B. 聚合仓自动发掘（REPO_QUERIES + 文件树）：搜 tvbox/影视/接口/订阅/drpy
     等关键词的仓库，拉默认分支文件树，把配置/线路清单文件收进来——
     新聚合仓每天出现，自动进池，摆脱对人工 CURATED_UPSTREAM 的依赖。
  C. 配置递归展开（expand）：对新发现的配置文件抓内容，提取其中引用的
     其它配置/清单 URL（ext/detail/spider 等字段里的 http(s) *.json/*.txt），
     深度 1 层——聚合配置互相引用是常态，一次展开=一次批量丰收。

输入：--base 旧 discovered.txt（上一轮累积的发现，保证搜索偶发失败时不丢存量）
输出：stdout 每行一个 raw 地址（供 ysc_build.py 消费），契约与 v1 相同。

排序策略：精选上游 > 高产配置池 > 本次新发现 > 无产出存量，取前 MAX_RESULTS 个。
「高产配置池」是 2026-10-13 加的反馈回路：读昨日提交的 全部.json/部分.json，
把产出过已验证源的配置 URL（站点 _cfg 字段，由 ysc_build.py 打戳）强制留在池
子里——没有它，搜索排序抖动会让昨天还在产品里的源今天就从 discovered.txt
消失，用户感知是「源越来越少」。新发现仍 guaranteed 插队在无产出存量之前。
"""
import os, sys, json, argparse, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (compatible; TVBoxAggregator/2.0)"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
# 自己仓库的产物不进发现池（A/B 路已按 dearloyal/ 前缀过滤，这里是 C 路防御）
SELF_RAW_RE = __import__("re").compile(
    r"^https?://raw\.githubusercontent\.com/dearloyal/CS/", __import__("re").I)

# ── A. 代码内容搜索：不依赖任何固定仓库，谁发布都能搜到 ──
QUERIES = [
    '"api.php/provide/vod" extension:json',
    '"api.php/provide/vod" tvbox extension:json',
    '"api.php/provide/vod" 影视 extension:json',
    '"api.php/provide/vod" 短剧 extension:json',
    'filename:luna-tv-config.json',
    'filename:LunaTV-config.json',
    '"api_site" "is_adult" extension:json',
    '"spider" "api.php/provide/vod" extension:json',
    '"csp_" "sites" extension:json',
    '"drpy" extension:json',
]

# ── B. 聚合仓自动发掘：搜仓库而非搜代码，覆盖整仓文件树 ──
REPO_QUERIES = [
    'tvbox in:name',
    'tvbox in:description',
    '影视仓 in:name,description',
    'FongMi in:name,description',
    'tvbox 接口 in:name,description',
    'drpy in:name',
    'hiker 影视 in:name,description',
]
MAX_REPO_PAGES = 1          # 每查询 1 页（50 个仓库）
MAX_REPOS_TREE = 60         # 每次运行最多拉多少个仓库的文件树
# 文件树里像"配置/线路清单"的路径：*.json/*.txt 且名字含这些特征
TREE_EXT_RE = __import__("re").compile(r"\.(json|txt)$", __import__("re").I)
TREE_NAME_RE = __import__("re").compile(
    r"(tvbox|luna|box|config|接口|线路|影视|订阅|饭太硬|fangao|list|单仓|聚合|源|短剧|duanju|catv|fongmi)",
    __import__("re").I)

# ── C. 配置递归展开 ──
MAX_CONFIG_FETCH = 120      # 每次运行最多展开多少个新配置
MAX_EXPAND_ADD = 250        # 展开最多带回多少个新容器 URL
URL_IN_CONFIG_RE = __import__("re").compile(r'https?://[^\s"\'<>\\)]+\.(?:json|txt)', __import__("re").I)
# 明显不是配置/清单的 URL（封面/图标/统计等）直接丢
EXPAND_DENY_RE = __import__("re").compile(
    r"(shields\.io|img\.shields|badge|cover|logo|icon|avatar|\.jpg|\.png|\.webp|\.gif|"
    r"travis|circleci|github\.com/[^/]+/[^/]+/(?:blob|tree|raw)/)", __import__("re").I)

PER_PAGE = 100              # 代码搜索每次 API 请求最多返回 100 条
MAX_PAGES_PER_QUERY = 3     # 每个查询最多翻 3 页（GitHub 代码搜索上限 1000 条）
MAX_RESULTS = 900           # 最终送给聚合器的配置文件上限（v1 为 400）
TIME_BUDGET = 540           # 全局软时限（秒）：CI discover job 上限 15 分钟，留足余量
T0 = time.time()

def out_of_time():
    return time.time() - T0 > TIME_BUDGET

# 精选上游：(仓库, 文件路径) —— 均为实测有产量且高频更新的公开聚合仓。
# 只登记仓库和路径，默认分支在运行时查询：上游从 main 切到 master 也不会 404。
# 自动发掘（B 路）起来后，这里是保底，不再是唯一入口。
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
CURATED_STARS = 10 ** 12

# ── 反馈回路：高产配置池 ──
# 流水线上游 job（output/probe）会把 全部.json/部分.json 提交回仓库，
# discover job 的 checkout 里就有昨日产物；站点带的 _cfg 字段（ysc_build.py
# 打戳）指明它来自哪个 discovered.txt 配置 URL。产出过已验证源的配置 URL
# 必须永远留在池子里，否则搜索排序一变，产品里的源就断流。
PRODUCT_FILES = ["全部.json", "部分.json"]


def norm_url(u):
    """配置 URL 轻量归一：小写 scheme/host、去尾部斜杠，用于跨日比对。"""
    try:
        sp = urllib.parse.urlsplit(u.strip())
        return urllib.parse.urlunsplit(
            (sp.scheme.lower(), sp.netloc.lower(), sp.path.rstrip("/"), sp.query, ""))
    except Exception:
        return u.strip().lower().rstrip("/")


def productive_urls():
    """从昨日订阅产物反推高产配置池（_cfg 反馈）。无产物文件时返回空集。"""
    prod = set()
    for f in PRODUCT_FILES:
        if not os.path.exists(f):
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                d = json.load(fp)
        except Exception:
            continue
        for s in d.get("sites", []):
            cfg = str(s.get("_cfg") or "").strip()
            if cfg.startswith("http"):
                prod.add(cfg)
    return prod


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


def search_code(query):
    """A 路：代码内容搜索 → [(raw_url, stars)]，失败时抛异常由调用方兜底。"""
    out = []
    for page in range(1, MAX_PAGES_PER_QUERY + 1):
        if out_of_time():
            break
        q = urllib.parse.quote(query)
        url = (f"https://api.github.com/search/code?per_page={PER_PAGE}"
               f"&page={page}&q={q}")
        try:
            data = api_get(url)
        except Exception as e:
            print(f"[discover] 查询失败 {query!r} page{page}: {e}", file=sys.stderr)
            break
        time.sleep(2.0)  # 主动限速，避免触发 429
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


def search_repos(query):
    """B 路第一步：仓库搜索 → [(full_name, stars, default_branch)]。"""
    out = []
    for page in range(1, MAX_REPO_PAGES + 1):
        q = urllib.parse.quote(query)
        url = (f"https://api.github.com/search/repositories?per_page=50"
               f"&page={page}&sort=updated&q={q}")
        try:
            data = api_get(url)
        except Exception as e:
            print(f"[discover] 仓库查询失败 {query!r}: {e}", file=sys.stderr)
            break
        time.sleep(2.0)
        for it in data.get("items", []):
            full = it.get("full_name") or ""
            if not full or full.startswith("dearloyal/"):
                continue
            if it.get("fork"):
                continue  # fork 仓内容与上游重复，跳过
            out.append((full, it.get("stargazers_count") or 0,
                        it.get("default_branch") or "main"))
        if len(data.get("items", [])) < 50:
            break
    return out


def tree_config_urls(repo, branch):
    """B 路第二步：拉仓库文件树，挑出像配置/清单的文件 → [raw_url]。"""
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
    try:
        data = api_get(url)
    except Exception as e:
        print(f"[discover] 拉取 {repo} 文件树失败: {e}", file=sys.stderr)
        return []
    out = []
    for node in data.get("tree", []):
        if node.get("type") != "blob":
            continue
        path = node.get("path") or ""
        if not TREE_EXT_RE.search(path):
            continue
        base = path.rsplit("/", 1)[-1]
        if not TREE_NAME_RE.search(base) and not TREE_NAME_RE.search(path):
            continue
        if node.get("size", 0) > 8 * 1024 * 1024:
            continue  # 超大文件多半不是配置
        p = "/".join(urllib.parse.quote(x) for x in path.split("/"))
        out.append(f"https://raw.githubusercontent.com/{repo}/{branch}/{p}")
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


def fetch_raw(url, timeout=15):
    """抓一个配置原文（C 路展开用）。失败返回空串。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2 * 1024 * 1024).decode("utf-8", "ignore")
    except Exception:
        return ""


def expand_configs(urls, found, base_set):
    """C 路：抓新配置原文，提取其中引用的其它 json/txt 容器 URL（depth=1）。

    只展开本次新发现、且尚未在池里的 URL；每个 URL 只展开一次
    （下一轮它就在 --base 里了，天然不会重复展开）。
    新 URL 直接写入 found（stars=0，靠"新发现"权重插队）。
    """
    todo = [u for u in urls if u not in found][:MAX_CONFIG_FETCH]
    if not todo:
        return 0
    added = 0

    def work(u):
        return u, URL_IN_CONFIG_RE.findall(fetch_raw(u))

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(work, u) for u in todo]
        for fu in as_completed(futs):
            if added >= MAX_EXPAND_ADD:
                break
            u, refs = fu.result()
            for r in refs:
                if added >= MAX_EXPAND_ADD:
                    break
                r = r.rstrip("\\.,;)")
                if EXPAND_DENY_RE.search(r):
                    continue
                if not r.startswith("http") or r in found:
                    continue
                if SELF_RAW_RE.match(r):
                    continue  # 自己仓库的产物（防御：第三方配置里引用时）
                if "raw.githubusercontent.com" in r and r.count("/") < 6:
                    continue  # 仓库首页类地址，不是文件
                found[r] = 0
                added += 1
    print(f"[discover] 递归展开 {len(todo)} 个新配置，带回 {added} 个容器 URL",
          file=sys.stderr)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="上一轮累积的 discovered.txt")
    ap.add_argument("--no-repo-search", action="store_true",
                    help="跳过 B 路聚合仓发掘（调试用）")
    ap.add_argument("--no-expand", action="store_true",
                    help="跳过 C 路递归展开（调试用）")
    ap.add_argument("--base-first", action="store_true",
                    help="存量优先于新发现（一次性兜底用：_cfg 反馈回路生效前，"
                         "保证旧配置不被新发现挤出池子；反馈回路常态运行后不需要）")
    args = ap.parse_args()

    found = {}  # raw -> stars
    base_set = set()
    # 1) 先载入存量（上一轮的发现），保证搜索失败时也不丢
    if args.base and os.path.exists(args.base):
        with open(args.base, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and line.startswith("http"):
                    found[line] = found.get(line, 0)
                    base_set.add(line)
        print(f"[discover] 存量发现 {len(found)} 个", file=sys.stderr)

    # 2) A 路：全网代码内容搜索
    total_new = 0
    for q in QUERIES:
        if out_of_time():
            print("[discover] 逼近时限，A 路提前收工", file=sys.stderr)
            break
        for raw, stars in search_code(q):
            if raw not in found:
                total_new += 1
            found[raw] = max(found.get(raw, 0), stars)
    print(f"[discover] A 路代码搜索：本次新发现 {total_new} 个，合计 {len(found)} 个",
          file=sys.stderr)

    # 3) B 路：聚合仓自动发掘（搜仓库 → 拉文件树 → 挑配置文件）
    if not args.no_repo_search:
        repos = []
        seen_repos = set()
        for q in REPO_QUERIES:
            if out_of_time():
                print("[discover] 逼近时限，B 路仓库搜索提前收工", file=sys.stderr)
                break
            for full, stars, br in search_repos(q):
                if full not in seen_repos:
                    seen_repos.add(full)
                    repos.append((full, stars, br))
        print(f"[discover] B 路仓库搜索命中 {len(repos)} 个仓，"
              f"按 star 取前 {MAX_REPOS_TREE} 个拉文件树", file=sys.stderr)
        n_tree = 0
        for full, stars, br in sorted(repos, key=lambda x: -x[1])[:MAX_REPOS_TREE]:
            for raw in tree_config_urls(full, br):
                if raw not in found:
                    total_new += 1
                found[raw] = max(found.get(raw, 0), stars)
                n_tree += 1
            if out_of_time():
                print("[discover] 逼近时限，B 路提前收工", file=sys.stderr)
                break
            time.sleep(0.15)
        print(f"[discover] B 路文件树收获 {n_tree} 个配置文件，合计 {len(found)} 个",
              file=sys.stderr)

    # 4) C 路：递归展开（只展开本次新配置，depth=1）
    if not args.no_expand and not out_of_time():
        new_urls = [u for u in found if u not in base_set and found.get(u, 0) < CURATED_STARS]
        expand_configs(new_urls, found, base_set)
        total_new = sum(1 for u in found if u not in base_set)
        print(f"[discover] C 路后合计 {len(found)} 个，其中新发现 {total_new} 个",
              file=sys.stderr)

    # 5) 精选上游：必选，不参与 star 竞争
    for u in curated_urls():
        found[u] = max(found.get(u, 0), CURATED_STARS)
    print(f"[discover] 加入精选上游 {len(CURATED_UPSTREAM)} 个", file=sys.stderr)

    # 5.5) 高产配置池（反馈回路）：昨日产物里产出过已验证源的配置 URL。
    #      今日搜索没再捞到它的（排序抖动/被限流），也必须补回池子——
    #      否则它对应的源今天就会从产品里消失。
    prod = productive_urls()
    prod_norm = {norm_url(u) for u in prod}
    n_prod_back = 0
    for u in prod:
        if u not in found:
            found[u] = 0
            n_prod_back += 1
    print(f"[discover] 高产配置池 {len(prod)} 个（补回未再搜到 {n_prod_back} 个）",
          file=sys.stderr)

    # 6) 排序：精选 > 高产池 > 本次新发现 > 无产出存量，取前 MAX_RESULTS 个
    #    --base-first：存量提到与新发现同档之前（一次性兜底，反馈回路未生效时用）
    def rank_key(kv):
        raw, stars = kv
        if stars >= CURATED_STARS:
            tier = 3
        elif norm_url(raw) in prod_norm:
            tier = 2
        elif args.base_first and raw in base_set:
            tier = 2
        elif raw not in base_set:
            tier = 1
        else:
            tier = 0
        return (tier, stars)

    ranked = sorted(found.items(), key=rank_key, reverse=True)
    out = [raw for raw, _ in ranked[:MAX_RESULTS]]
    n_out_prod = sum(1 for raw in out if norm_url(raw) in prod_norm)
    for raw in out:
        print(raw)
    print(f"[discover] 输出 {len(out)} 个配置 URL（上限 {MAX_RESULTS}，"
          f"含高产 {n_out_prod} 个）", file=sys.stderr)


if __name__ == "__main__":
    main()
