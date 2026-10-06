# ceshi —— TVBox 影视仓 终极兜底聚合（杀手锏）

这是给影视仓（TVBox）用的**最后一道保险**：当 `yingshicang`、`quanwangjiansuo` 都不可用、或你想一次性拿到最大覆盖时，订阅这个仓库。

## 它每天做什么（任务时间已完善）

| 步骤 | 内容 |
|---|---|
| ① 先抓 yingshicang | 直接拉 `yingshicang` 的成品（normal / adult 两份，已含上游老源 + 探测去重 + 分类） |
| ② 再抓 quanwangjiansuo | 直接并入 `quanwangjiansuo` 的自搜结果（normal / adult 两份） |
| ③ 自己再搜一遍 | 用 GitHub 内容搜索（`"api.php/provide/vod"` 等特征）自主发现新发布的配置 |
| ④ 去重 | 全部按接口地址（api）去重，重叠只留一份 |
| ⑤ 合并分类 | 按 `is_adult` / `🔞` / 关键词 分为**不含成人**和**纯成人**两份 |
| ⑥ 保守探测 | 仅剔除确定失效（DNS失败/404/5xx）；超时一律保留；剔除比例 >30% 触发安全阀 |

**时间安排**：
- `quanwangjiansuo` 每天**北京时间 02:00**（UTC 18:00）先跑，产出最新自搜结果；
- 本任务安排在它之后，**北京时间 03:00**（UTC 19:00）运行，确保吃到当天新鲜结果；
- 即使 `quanwangjiansuo` 当天失败，本任务仍会自己再搜一遍兜底。
- 也可在 GitHub → Actions → 本工作流 → `Run workflow` 手动触发。

## 订阅链接（单独成行，方便复制）

```
https://ghfast.top/https://raw.githubusercontent.com/dearloyal/ceshi/main/tvbox-normal.json
```

```
https://ghfast.top/https://raw.githubusercontent.com/dearloyal/ceshi/main/tvbox-adult.json
```

备用镜像（把域名换掉即可）：`https://fastly.jsdelivr.net/gh/dearloyal/ceshi@main/tvbox-normal.json`（末尾换成 `tvbox-adult.json`）。

## 说明

- 影视源没有中央注册表，`ceshi` 通过**监控已知发布点 + 自主内容搜索**实现"最大覆盖"，这是上游聚合器的标准做法。
- 境外 Runner 连国内源常超时，探测刻意保守（误删比漏删更糟），真实存活率以你手机端打开为准。
