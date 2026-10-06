# CS（整合 · 测试兜底）

影视仓 TVBox 配置仓库，三仓库里的**整合层**。

## 角色：整合兜底
- **本身不自己搜源**。它只把下面两个仓库已经产出的成果拿过来**去重 + 合并**，作为最后一道保险。
- 平时**不用**，只有 YSC / QWJS 都不可用时的最后手段。

## 它整合谁（级联顺序）
1. **YSC**（日常主用）：每天 01:00 从 3 个上游仓库抓取并分流
2. **QWJS**（保底备选）：每天 02:00 自主全网搜 GitHub 配置
3. **CS**（本仓库）：每天 03:00 把上面两者的成果去重合并 → 分类 → 保守探测

> 全网自主搜索只由 QWJS 负责一次，CS 只做整合，不重复搜。

## 自动更新
- 北京时间 **03:00**（GitHub Actions 定时，排在 YSC / QWJS 之后）

## 订阅链接（每个单独复制）
普通版：
```
https://ghfast.top/https://raw.githubusercontent.com/dearloyal/CS/main/tvbox-normal.json
```
成人版：
```
https://ghfast.top/https://raw.githubusercontent.com/dearloyal/CS/main/tvbox-adult.json
```

## 规模
- 只保留 normal / adult 两版（已去重整合，只留标准 api 接口，无废源）
