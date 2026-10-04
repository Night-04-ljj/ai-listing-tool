"""
平台规则 + 品牌规范 + 关键词库。

【这个文件是整个工具的灵魂】
因为「AI 写的文案能不能直接上线」取决于这里，而不是取决于模型多强。
一句话记住：
    「模型负责写，规则负责卡。生成 10 条文案不难，难的是保证 10 条都能过审。」

⚠️ 下面这些限制是亚马逊的通用规则，**具体类目和站点会在卖家后台有更细的要求**，
   上线前必须按 Seller Central 的实际要求核对。所以这里做成可配置的表。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from config import DATA_DIR

# ---------------------------------------------------------------- 站点规则

SITE_RULES: dict[str, dict] = {
    "US": {
        "language": "English (US)",
        "locale_hint": "美式英语拼写（color / mounting / aluminum），面向美国消费者",
        "title_style": "用逗号分隔卖点组；典型结构：品牌 + 品类型号 + 适配尺寸, 核心卖点, 硬参数, 颜色",
        "title_target_ratio": 0.90,
        "title_max": 200,
        "bullet_count": 5,
        "bullet_max": 500,
        "bullet_recommended": 200,
        "description_max": 2000,
        "search_terms_bytes": 250,
    },
    "UK": {
        "language": "English (UK)",
        "locale_hint": "英式英语拼写（colour / mounting / aluminium），面向英国消费者",
        "title_style": "用逗号分隔卖点组；典型结构：品牌 + 品类型号 + 适配尺寸, 核心卖点, 硬参数, 颜色",
        "title_target_ratio": 0.90,
        "title_max": 200,
        "bullet_count": 5,
        "bullet_max": 500,
        "bullet_recommended": 200,
        "description_max": 2000,
        "search_terms_bytes": 250,
    },
    "DE": {
        "language": "German",
        "locale_hint": "地道德语，不要英文直译；德语复合词要自然，尺寸单位用 cm/kg",
        # ⚠️ 这一条来自真实在售标题的观察，不是我的猜测：
        #    Amazon.de 上 FITUEYES 的标题用 | 把卖点分成几组，例如
        #    "FITUEYES TV Ständer Rollbar für 24–48 Zoll, bis zu 35kg, Master Serie |
        #     TV Standfuss, VESA Max. 200x200mm, Höhenverstellbar, mit Rollen & Kabelmanagement,
        #     für Wohnung Büro Heim, Weiß"
        "title_style": "德语标题常用 | 把卖点分成 2 到 3 组；典型结构："
                      "品牌 + 品类 + 适配尺寸 + 承重 + 系列名 | 硬参数 + 功能 | 使用场景 + 颜色",
        "title_target_ratio": 0.93,
        "title_max": 200,
        "bullet_count": 5,
        "bullet_max": 500,
        "bullet_recommended": 200,
        "description_max": 2000,
        "search_terms_bytes": 250,
    },
    "JP": {
        "language": "Japanese",
        "locale_hint": "自然日语，敬体（です・ます），不要中文汉字直搬；尺寸标注要符合日本习惯",
        "title_style": "日语标题常用空格分隔卖点组；典型结构：品牌 + 品类 + 适配尺寸 + 承重 + VESA + 主要功能",
        "title_target_ratio": 0.90,
        "title_max": 200,
        "bullet_count": 5,
        "bullet_max": 500,
        "bullet_recommended": 200,
        "description_max": 2000,
        "search_terms_bytes": 250,
    },
}

# ---------------------------------------------------------------- 禁用内容

# 促销 / 主观夸大 / 绝对化承诺 —— 亚马逊明确禁止出现在标题和五点描述里
BANNED_PHRASES: list[str] = [
    "best seller", "bestseller", "best-seller", "best selling", "top seller",
    "top rated", "number one", "#1", "no.1", "hot item", "hot sale", "hottest",
    "sale", "on sale", "discount", "cheap", "cheapest", "lowest price",
    "free shipping", "free delivery", "money back", "money-back",
    "guarantee", "guaranteed", "warranty included", "100% satisfaction",
    "lifetime warranty", "risk free", "risk-free",
    "buy now", "order now", "limited time", "while supplies last",
    "new arrival", "brand new",  # 状态类描述应由系统字段体现，不写进文案
    "eco friendly", "eco-friendly", "environmentally friendly",  # 需认证才能宣称
    "waterproof", "water proof",  # 需按类目实际防护等级宣称
    "fda approved", "fda-approved", "cures", "treats", "prevents disease",
    # 德语常见违规
    "bestseller", "angebot", "rabatt", "kostenloser versand", "garantie",
    # 日语常见违规
    "送料無料", "激安", "最安値", "絶対",
]

# 亚马逊页面不允许出现的特殊字符（各站点略有差异，按需调整）
FORBIDDEN_CHARS = set("!?$_{}^¬¦~<>[]")

# 允许全大写的行业缩写，其余 4 个字母以上的全大写词都会被标记为违规
CAPS_ALLOWLIST = {
    "VESA", "HDMI", "USB", "LED", "LCD", "OLED", "QLED", "HDR", "TV", "AV",
    "SPDIF", "RCA", "MM", "CM", "KG", "LB", "LBS", "INCH", "IN", "IPS", "PWM",
}

# 竞品 / 第三方品牌名：在文案里提及他人商标是常见违规
COMPETITOR_BRANDS: list[str] = [
    "samsung", "lg", "sony", "vizio", "tcl", "hisense", "philips", "panasonic",
    "sharp", "toshiba", "roku", "fire tv", "chromecast", "apple tv", "xiaomi",
    "skyworth", "changhong", "haier", "hitachi",
]

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)


# ---------------------------------------------------------------- 品牌与关键词库

def load_brand_guide() -> dict:
    path = DATA_DIR / "brand_guide.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少品牌规范文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_keywords() -> dict:
    path = DATA_DIR / "keywords.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少关键词库文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# 哪些站点是英语站点。
# 【为什么需要这个】原来的写法是「找不到就回退到 US」，看起来很方便，其实是个雷：
#   实测 monitor_mount / av_furniture 在 UK 站点缺词库，就静默回退成了 US 英文词。
#   现在只是 UK 缺，勉强能接受；但一旦加法语/意大利语站点，
#   就会把**英文关键词塞进法语 Listing**，直接毁掉文案质量，而且不报错、很难发现。
# 所以规则改成：**只允许同为英语的站点之间互相回退，其他语言缺了就返回空**。
ENGLISH_SITES = {"US", "UK"}


def keywords_for(category: str, site: str) -> list[str]:
    """取某个类目在某站点的目标关键词。

    【设计要点：这里就是「不需要 RAG」的证据】
    关键词库现在只有几十条，直接全量放进 Prompt。什么时候需要检索？
    当它涨到几千条、Prompt 装不下的时候。那时我会先考虑「按类目过滤 + 关键词匹配」，
    而不是一上来就上向量库——**先用最简单能解决问题的方案。**
    """
    cat = load_keywords().get(category, {})
    got = cat.get(site)
    if got:
        return got
    # 只在同为英语的站点之间回退（US <-> UK），其他语言绝不回退到英文
    if site in ENGLISH_SITES:
        for other in sorted(ENGLISH_SITES):
            if other != site and cat.get(other):
                return cat[other]
    return []


def keyword_source(category: str, site: str) -> str:
    """说明关键词是从哪来的，用于在日志和报告中如实标注。"""
    cat = load_keywords().get(category, {})
    if cat.get(site):
        return f"{site} 专用词库"
    if site in ENGLISH_SITES:
        for other in sorted(ENGLISH_SITES):
            if other != site and cat.get(other):
                return f"回退到 {other}（同为英语站点）"
    return "无词库 —— 由模型按目标语言自行生成"


# ---------------------------------------------------------------- 关键词覆盖度

# 【这里有一个我自己踩过的坑，是很好的经验】
# 我一开始算「关键词覆盖率」用的是**整短语匹配**：把 "heavy duty tv wall mount"
# 当成一个字符串，看它有没有原样出现在文案里。
# 结果覆盖率只有 12% —— 但文案其实把 heavy / duty / wall / mount 都写进去了，
# 而且标题里就有 "Heavy Duty ... Bracket" 和 "TV Wall Mount"。
#
# **是指标错了，不是文案差了。** 搜索引擎是按词（含词干）匹配的，不会要求你
# 原样输出整个长尾短语。所以我改成词级覆盖：
#   覆盖率 = 关键词里「有实质意义的词」出现在文案中的比例，再对所有关键词取平均。
# 这件事让我记住一句话：**指标定错了，你会朝着错误的方向优化。**

_STOPWORDS = {
    "for", "the", "and", "with", "a", "an", "of", "to", "in", "on", "your",
    "this", "that", "is", "are", "or", "by", "der", "die", "das", "und",
    "für", "fur", "mit", "von", "den", "dem", "ein", "eine",
}

# ⚠️ 这个正则有讲究：必须是 Unicode 感知的。
# 最早我写的是 r"[a-z0-9]+"，结果德语 "Ständer" 被从 ä 处切成 "st" + "nder"，
# 而 "st" 这种两字母碎片几乎能匹配任何英文文本 —— **覆盖率被严重高估**。
# 法语 é、德语 ü/ß、日语假名同理，都必须当成词的一部分。
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def keyword_words(keyword: str) -> list[str]:
    """把关键词拆成有实质意义的词（去掉介词、冠词这类停用词）。

    注意：是**按 Unicode 字母**切，不是按 ASCII 字母切。
    """
    return [
        w for w in _WORD_RE.findall(keyword.lower())
        if len(w) >= 2 and w not in _STOPWORDS
    ]


def keyword_coverage_score(keyword: str, body_lower: str) -> float:
    """单个关键词的覆盖度，0.0 ~ 1.0。整短语原样出现直接给 1.0。"""
    kw = keyword.lower().strip()
    if not kw:
        return 1.0
    if kw in body_lower:
        return 1.0
    words = keyword_words(kw)
    if not words:
        return 0.0
    return sum(1 for w in words if w in body_lower) / len(words)


def keyword_covered(keyword: str, body_lower: str) -> bool:
    """关键词里所有实词都出现了，才算覆盖。"""
    return keyword_coverage_score(keyword, body_lower) >= 1.0


def coverage_of(keywords: list[str], texts: list[str]) -> float:
    """一组关键词相对一段正文的整体覆盖度（0.0 ~ 1.0）。"""
    if not keywords:
        return 1.0
    body = " ".join(texts).lower()
    return sum(keyword_coverage_score(k, body) for k in keywords) / len(keywords)
