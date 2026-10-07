#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QWJS 分类脚本（部署到 dearloyal/QWJS → scripts/classify.py）
==============================================================
任务（按用户要求）：
  读取 YSC 产出的「全部网站接口」
    （dearloyal/YSC/main/网站接口数据.json，可用环境变量 YSC_JSON 覆盖为本地路径做测试），
  ① 用【直连脚本】与【爬虫脚本】分别判定接口类型；
  ② 按【成人 / 正常】标注（全部 = 正常接口；部分 = 成人接口）；
  ③ 分成 4 份文件，并在文件内明确标注：
        1直连-全部.json : 直连 + 正常接口（全部）
        1直连-部分.json : 直连 + 成人接口（部分）
        2爬虫-全部.json : 爬虫 + 正常接口（全部）
        2爬虫-部分.json : 爬虫 + 成人接口（部分）
  标注说明：
        全部 = 正常可用接口；部分 = 成人接口。
运行：python3 scripts/classify.py
"""
import json, os, re, sys, time, subprocess

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
YSC_URL = os.environ.get(
    "YSC_JSON",
    "https://raw.githubusercontent.com/dearloyal/YSC/main/网站接口数据.json")
# 合并管线场景：上游产物已在同一工作目录，直接读本地（可用 LOCAL_YSC 覆盖）
LOCAL_YSC = os.environ.get("LOCAL_YSC", "网站接口数据.json")

ADULT_KW = re.compile(
    r"(18|r18|成人|🔞|色|黄|麻豆|艾旦|sex|av|番号|福利|伦理|裸|春药|约炮|同志|同性|"
    r"avb|porn|hentai|jm|萝莉)", re.I)
CRAWLER_API = re.compile(r"^(csp_|drpy|spider|\./|jar:|js:|py:|http.*\.js$|http.*\.py$)", re.I)


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
    """优先读同仓库本地文件（合并管线），否则回退 raw 拉取（多仓链路）。
    这样同一份脚本在两种部署形态下都能跑，方便回退。"""
    if os.path.exists(LOCAL_YSC):
        with open(LOCAL_YSC, encoding="utf-8") as f:
            print(f"[QWJS] 读取本地文件: {LOCAL_YSC}")
            return json.load(f)
    print(f"[QWJS] 本地无 {LOCAL_YSC}，回退拉取: {YSC_URL}")
    return fetch_json(YSC_URL)


def is_crawler(api):
    api = str(api or "").strip()
    if not api:
        return True
    return bool(CRAWLER_API.match(api)) or (not api.lower().startswith("http"))


def adult_of(site):
    # 优先用 YSC 已标注的字段，缺则重算
    if isinstance(site.get("_adult"), bool):
        return site["_adult"]
    blob = " ".join(str(site.get(k, "")) for k in ("name", "key", "api", "type", "ext"))
    return bool(ADULT_KW.search(blob))


def crawler_of(site):
    if isinstance(site.get("_crawler"), bool):
        return site["_crawler"]
    return is_crawler(site.get("api"))


def main():
    d = load_input()
    sites = d.get("sites", [])
    print(f"[QWJS] 读取上游接口数: {len(sites)}")

    direct = [s for s in sites if not crawler_of(s)]
    crawler = [s for s in sites if crawler_of(s)]
    print(f"  直连 {len(direct)} | 爬虫 {len(crawler)}")

    d_all = [s for s in direct if not adult_of(s)]
    d_part = [s for s in direct if adult_of(s)]
    c_all = [s for s in crawler if not adult_of(s)]
    c_part = [s for s in crawler if adult_of(s)]
    print(f"  直连-全部 {len(d_all)} | 直连-部分 {len(d_part)} | "
          f"爬虫-全部 {len(c_all)} | 爬虫-部分 {len(c_part)}")

    def write(fname, category, subset, lst):
        meta = {
            "name": f"{category}接口 - {subset}（{'正常' if subset == '全部' else '成人'}）",
            "category": category,            # 直连 / 爬虫
            "subset": subset,                # 全部 / 部分
            "subset_meaning": "全部 = 正常可用接口" if subset == "全部" else "部分 = 成人接口",
            "adult": subset == "部分",       # 标注：部分即成人
            "source": "dearloyal/YSC",
            "count": len(lst),
        }
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({**meta, "sites": lst}, f, ensure_ascii=False, indent=2)
        print(f"  写出 {fname} ({len(lst)})")

    write("1直连-全部.json", "直连", "全部", d_all)
    write("1直连-部分.json", "直连", "部分", d_part)
    write("2爬虫-全部.json", "爬虫", "全部", c_all)
    write("2爬虫-部分.json", "爬虫", "部分", c_part)
    print("[QWJS] 完成")


if __name__ == "__main__":
    main()
