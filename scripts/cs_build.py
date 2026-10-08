#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS 出链脚本（部署到 dearloyal/CS → scripts/build.py）
====================================================
任务（按用户要求）：
  ① 读取本仓 classify 阶段产出的 4 个分类接口；
  ② 读取本仓 build 阶段产出的「接口合集」；
  ③ 在 CS 仓库落地【5 个可订阅链接】，供用户直接使用：
        1. 1直连-全部.json   （直连 · 全部）
        2. 1直连-部分.json   （直连 · 部分）
        3. 2爬虫-全部.json   （爬虫 · 全部）
        4. 2爬虫-部分.json   （爬虫 · 部分）
        5. 接口合集.json     （可手动选线路 饭太硬/南风/王二小…）
  另外生成 README.md，列出 5 个订阅链接（对外不写分组含义）。
运行：python3 scripts/build.py
"""
import json, os, time, subprocess, sys

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# 单仓合并后，5 个文件同属 dearloyal/CS；旧 YSC / QWJS 仓已删除，
# 此处不再保留对其 raw 地址的引用（历史遗留会在产物里泄露旧仓名）。
SELF_BASE = os.environ.get("SELF_BASE", "https://raw.githubusercontent.com/dearloyal/CS/main/")

# (本地文件名, 上游base, 上游文件名)
ITEMS = [
    ("1直连-全部.json", SELF_BASE, "1直连-全部.json"),
    ("1直连-部分.json", SELF_BASE, "1直连-部分.json"),
    ("2爬虫-全部.json", SELF_BASE, "2爬虫-全部.json"),
    ("2爬虫-部分.json", SELF_BASE, "2爬虫-部分.json"),
    ("接口合集.json", SELF_BASE, "接口合集.json"),
]


def fetch_json(url, timeout=40):
    from urllib.parse import quote
    url = quote(url, safe=":/")  # 中文文件名需百分号编码（GitHub raw）
    for _ in range(3):
        try:
            r = subprocess.run(
                ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
                capture_output=True, text=True, timeout=timeout + 15)
            return json.loads(r.stdout), None
        except Exception as e:
            last = e
            time.sleep(2)
    return None, str(last)


def load_item(local, base, remote):
    """优先读同仓库本地文件（合并管线），否则回退 raw 拉取（多仓链路）。"""
    if os.path.exists(local):
        try:
            with open(local, encoding="utf-8") as f:
                print(f"  · 读取本地 {local}")
                return json.load(f), None
        except Exception as e:
            return None, f"本地读取失败: {e}"
    return fetch_json(base + remote)


def main():
    t0 = time.time()
    landed = []
    for local, base, remote in ITEMS:
        d, err = load_item(local, base, remote)
        if d is None:
            print(f"  ✗ 获取 {remote} 失败: {err}")
            continue
        d = dict(d)
        d["_from"] = base + remote
        n = len(d.get("sites", [])) if "sites" in d else len(d.get("lines", []))
        with open(local, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        landed.append((local, n))
        print(f"  ✓ 落地 {local} (items={n})")

    # README 由部署脚本（deploy.py）单独维护为「模糊描述 + 声明」版本。
    # 此处刻意不写入 README —— 历史踩坑：流水线每次运行都会把对外说明冲掉，
    # 重新生成成带用途说明和链接的版本，等于白改。故停用 build_readme()。
    print(f"[CS] 完成，落地 {len(landed)}/5 个文件，用时 {(time.time()-t0):.1f}s")


def build_readme(landed):
    lines = [
        "# CS · 影视仓最终出链（ceshi）",
        "",
        "> 本仓库是「终极兜底」出链层：把 QWJS 分类好的接口 + YSC 的线路合集，",
        "> 整理成 5 个可直接订阅的影视仓链接。每周一自动更新。",
        "",
        "## 五个订阅链接（ghfast 镜像，国内可用）",
        "",
    ]
    labels = {
        "1直连-全部.json": "① 直连 · 全部",
        "1直连-部分.json": "② 直连 · 部分",
        "2爬虫-全部.json": "③ 爬虫 · 全部",
        "2爬虫-部分.json": "④ 爬虫 · 部分",
        "接口合集.json": "⑤ 接口合集（可手动选线路：饭太硬/南风/王二小…）",
    }
    for local, n in landed:
        gh = f"https://raw.githubusercontent.com/dearloyal/CS/main/{local}"
        fast = f"https://ghfast.top/{gh}"
        lines.append(f"### {labels.get(local, local)}  （{n} 项）")
        lines.append(f"- 官方：`{gh}`")
        lines.append(f"- 镜像：`{fast}`")
        lines.append("")
    lines += [
        "## 备注",
        "- 全部 / 部分为接口的不同分组，按使用需要选用。",
        "- 直连 / 爬虫 由 QWJS 用直连脚本与爬虫脚本判定后分流。",
        "- 接口合集来自 YSC：导入影视仓后可在线路列表手动切换 饭太硬 / 南风 / 王二小 等。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
