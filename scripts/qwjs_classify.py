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
  ③ 排序：全部.json 把「闪电资源」置顶（它有分类），部分.json 把「老色逼」置顶。

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
PIN_ALL_FIRST = "闪电资源"
PIN_PART_FIRST = "老色逼"

# 标志前缀
FLAG_NORMAL = "影视 "
FLAG_ADULT = "18 "

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


def main():
    d = load_input()
    sites = d.get("sites", [])
    print(f"[QWJS] 读取上游接口数: {len(sites)}")

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

    # ④ 置顶规则
    normal = pin_first(normal, PIN_ALL_FIRST)
    adult = pin_first(adult, PIN_PART_FIRST)

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
