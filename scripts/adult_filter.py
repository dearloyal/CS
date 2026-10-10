# -*- coding: utf-8 -*-
"""成人源判定（分层关键词，尽量少误伤）。

设计要点——为什么不是一个大正则：
  1) 中文强词只在「站点名 + 接口地址」上匹配，**不搜 key**。
     原因：爬虫站常把标题塞进 key（如 key=lfjs_番茄 但站名叫"影片库｜西虹视频"），
     搜 key 会把一堆正常站误判成成人。
  2) 纯字母词（jkun / 鸡坤 / 爱坤）额外搜 key，因为这类站常只在 key 里露出来。
  3) 数字「19」「18」**只匹配站点名且必须独立成词**（前后不为数字/字母）。
     原因：1905/1920/1818/183933/188/189 影视、919 体育、以及大量 md5/uuid key、
     哈希串里都含 "19"/"18"，全量搜会一次性误伤一大片正常源（小乐/小咕/采集/影视/
     克隆/酷我/咪咕… 都曾被嵌在域名里的 18 误判）。独立成词后只认 玉兔(18)/18哈/18av、
     19 等真成人。
  4) mojibake 兜底：上游有些站点名是 UTF-8 被误按 Latin-1 解码的乱码
     （显示为一串欧洲字母，实际是中文），先还原原文再参与判定。

宽泛开关：环境变量 ADULT_MODE=loose 时退化为"含既定词即成人"（包含 19/番茄/极品
的全部命中），用于需要宁可错杀的场景。
"""
import os
import re

# ── 通用强特征（沿用旧表） ─────────────────────────────────────────────
# 注：原表里的 `18` / `jm` 已移出，改为更精准的边界匹配（见下方 NAME_NUM_18 / JM_KW）：
#   · `18` 嵌在域名/哈希里（1818 / 183933 / 188 / 189 / 18b8…）会误伤一大批正常站，
#     只认「站点名里独立成词的 18」（玉兔(18)、18哈、18av 等真成人）。
#   · `jm` 在 api 密文 / 拼音 key 里（影视工厂 XXjM…、追剧喵 zjm_spider）误伤，且无任何
#     真成人靠 jm 命中，直接移除以修掉 影视工厂 / 追剧喵 的误判。
ADULT_KW = re.compile(
    r"(r18|成人|🔞|色|黄|麻豆|艾旦|sex|av|番号|福利|伦理|裸|春药|约炮|同志|同性|"
    r"avb|porn|hentai|萝莉)", re.I)

# ── 用户指定清单：中文词（番茄/极品带否定后缀，排除同名正常站） ──────────
CN_KW = re.compile(
    r"(小鸡|蝙蝠|精东|美少女|香奶儿?|桃花|乐播|滴滴|嘿嘿|废柴|玉兔|淫水机|越南"
    r"|大奶子|杏吧"
    r"|番茄(?!动漫|小说)|极品(?!┃听书|影视))"
)
# ── 用户指定清单：字母/别名词 ───────────────────────────────────────────
# ck 不裸搜（会误伤 bitbucket 的 "bucket"、track、luck 等），只匹配成人专用
# token：ckzy=CK资源、hsck=黄色仓库 这批域名，实测 9 条全为成人、零误伤。
EN_KW = re.compile(r"(jkun|鸡坤|爱坤|ckzy|hsck)", re.I)
# ── 用户指定清单：站点名里的独立 19 / 18（前后不为字母数字才算独立成词） ──
# 只认站点名（不认 api/key），避免 1905/1920/1818/183933/188/189/哈希串 误伤。
NAME_NUM_KW = re.compile(r"(?<![0-9a-z])19(?![0-9a-z])", re.I)
NAME_NUM_18 = re.compile(r"(?<![0-9a-z])18(?![0-9a-z])", re.I)

# ── mojibake 识别 ──────────────────────────────────────────────────────
# 典型乱码头：Ã Â ç å 等 Latin-1 区高位字符，紧跟一个控制/符号区的续字节
_MOJI_HINT = re.compile(
    r"[ÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõöøùúûüý]"
    r"[\x80-\xbf‚„…†‡ˆ‰‹Œ‘’“”•–—˜™\xa0]"
)


def demojibake(s):
    """把「UTF-8 被误按 Latin-1 解码」的乱码还原成中文；无需还原则返回 ''。"""
    if not s or not _MOJI_HINT.search(s):
        return ""
    try:
        fixed = s.encode("latin-1", "ignore").decode("utf-8", "strict")
    except Exception:
        return ""
    return fixed if fixed and fixed != s else ""


LOOSE = os.environ.get("ADULT_MODE", "").lower() == "loose"
_LOOSE_KW = re.compile(
    r"(小鸡|蝙蝠|精东|美少女|香奶儿?|桃花|乐播|滴滴|嘿嘿|废柴|玉兔|淫水机|越南|大奶子|杏吧"
    r"|番茄|极品|ckzy|hsck|jkun|鸡坤|爱坤|19|18)", re.I)


def is_adult(site):
    """判断一个 site dict 是否为成人源，返回 bool。"""
    name = str(site.get("name", "") or "")
    key = str(site.get("key", "") or "")
    api = str(site.get("api", "") or "")
    extless = " ".join(str(site.get(k, "")) for k in ("name", "key", "api"))

    # ext 多为加密串（openssl 密文），里面会随机出现 av/jm 这类双字母组合，
    # 让它参与匹配会持续误伤正常站，故通用正则一律不搜 ext。
    if ADULT_KW.search(extless):
        return True

    if LOOSE:
        return bool(_LOOSE_KW.search(extless))

    # 乱码名先还原再一起判
    fixed = demojibake(name)
    name_field = (name + " " + fixed) if fixed else name

    if CN_KW.search(name_field) or CN_KW.search(api):
        return True
    if EN_KW.search(name_field) or EN_KW.search(key) or EN_KW.search(api):
        return True
    if NAME_NUM_KW.search(name_field) or NAME_NUM_18.search(name_field):
        return True
    return False
