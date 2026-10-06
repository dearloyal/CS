# CS（测试 / ceshi）

> 角色：**杀手锏兜底** —— 平时不用，只有 YSC、QWJS 都不行时，最后才用。

## 是什么
三路合并版（终极兜底）：把 **YSC 成品 + QWJS 成品 + 自己再全网搜一遍** 合并、去重、分类，产出最全的 normal/adult 两版。

## 自动更新
- GitHub Actions 定时：**北京 03:00**（UTC `0 19 * * *`）
- 每天自动跑（在 YSC、QWJS 之后，形成级联 01→02→03）

## 订阅链接（整行复制进 TVBox）
普通版：
```
https://ghfast.top/https://raw.githubusercontent.com/dearloyal/CS/main/tvbox-normal.json
```
成人版：
```
https://ghfast.top/https://raw.githubusercontent.com/dearloyal/CS/main/tvbox-adult.json
```

## 产出规模
- normal ≈ 1340 源 / adult ≈ 157 源

## 合并顺序（scripts/aggregate.py）
1. 拉 **YSC** 成品（日常主用源）
2. 拉 **QWJS** 成品（保底源）
3. 自己**再全网搜一遍**
4. 去重 → 分类（normal/adult）→ 保守探测（只剔确定失效，>30% 触发安全阀）

## 注意事项
- 仓库内部引用已用新名：拉 YSC=`dearloyal/YSC`、QWJS=`dearloyal/QWJS`（改名前是 yingshicang/quanwangjiansuo）。
- `secrets.PAT` 为账号级凭据，改名不影响。

---
*关联：YSC（日常主用）、QWJS（保底）。整套项目设定已存入知识库长期记忆。*
