"""
数据结构（自建 Schema 校验，零依赖）+ 平台规则校验器。

【为什么不用 Pydantic】
pydantic 会带进 pydantic-core 等 4 个包，而这个结构只有 4 个字段。
更重要的是：**能少一个依赖，就少一个装不上的理由**。
这个项目现在的依赖是 **0** —— 不用 pip、不用虚拟环境，有 Python 就能跑。
面试时这是一句很硬的话：
    「我没引入 Pydantic，因为这个结构只有 4 个字段，手写校验更透明，
      而且让整个项目零依赖 —— 在现场任何一台机器上都能直接演示。」

【为什么这是整个工具最值钱的部分】
大模型生成文案，10 条里可能有 3 条超字符、有 1 条写了 "best seller"。
人工一条条查，等于没提效。
所以流程必须是：**生成 → 机器校验 → 把具体违规项回喂给模型重写 → 再校验**。

这段代码面试时直接打开给面试官看，比讲十分钟都有用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rules import (
    BANNED_PHRASES,
    CAPS_ALLOWLIST,
    COMPETITOR_BRANDS,
    EMOJI_RE,
    FORBIDDEN_CHARS,
    SITE_RULES,
    keyword_covered,
)


# ---------------------------------------------------------------- 数据结构

class SchemaError(ValueError):
    """模型输出的结构不对（缺字段、类型错、条数不对）。"""


class ListingDraft:
    """模型必须输出的结构。

    脏数据进不来：字段缺失、类型不对、bullets 不是字符串列表，都会抛 SchemaError，
    然后被 generate.py 捕获并触发「重写一轮」。
    """

    FIELDS = ("title", "bullets", "description", "search_terms")

    def __init__(self, title: str = "", bullets: list | None = None,
                 description: str = "", search_terms: str = "", **extra) -> None:
        self.title = self._as_str("title", title)
        self.bullets = self._as_str_list("bullets", bullets)
        self.description = self._as_str("description", description)
        self.search_terms = self._as_str("search_terms", search_terms)
        self.extra_fields = sorted(extra)  # 模型多给的字段，忽略但不报错

    # ---- 类型校验 ----
    @staticmethod
    def _as_str(name: str, value) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise SchemaError(f"字段 {name} 应该是字符串，实际是 {type(value).__name__}")
        return value

    @staticmethod
    def _as_str_list(name: str, value) -> list[str]:
        if value is None:
            raise SchemaError(f"缺少字段 {name}")
        if isinstance(value, str):
            raise SchemaError(f"字段 {name} 应该是字符串数组，实际是一个字符串")
        if not isinstance(value, (list, tuple)):
            raise SchemaError(f"字段 {name} 应该是字符串数组，实际是 {type(value).__name__}")
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, str):
                raise SchemaError(f"{name}[{i}] 应该是字符串，实际是 {type(item).__name__}")
            out.append(item)
        return out

    def model_dump(self) -> dict:
        """和 pydantic 的方法名保持一致，方便将来换回去。"""
        return {k: (list(v) if isinstance(v, list) else v) for k, v in
                ((f, getattr(self, f)) for f in self.FIELDS)}

    def __repr__(self) -> str:
        return f"ListingDraft(title={self.title[:40]!r}..., bullets={len(self.bullets)}条)"


# ---------------------------------------------------------------- 违规项

@dataclass
class Violation:
    field: str          # title / bullets[2] / description / search_terms
    rule: str           # 规则名，方便统计
    message: str        # 人话说明
    severity: str = "error"  # error 必须改；warning 建议改

    def to_prompt(self) -> str:
        return f"- [{self.field}] {self.message}"


def has_errors(violations: list[Violation]) -> bool:
    return any(v.severity == "error" for v in violations)


def format_violations(violations: list[Violation]) -> str:
    if not violations:
        return "  ✓ 全部规则通过"
    out = []
    for v in violations:
        mark = "✗" if v.severity == "error" else "!"
        out.append(f"  {mark} [{v.field}] ({v.rule}) {v.message}")
    return "\n".join(out)


# ---------------------------------------------------------------- 校验

# Unicode 感知的词正则：德语 Ständer / 法语 é / 日语假名都要算一个词。
# 用 [a-z]{4,} 会把 "Ständer" 从 ä 处切开，切出 'nder' 这种无意义的碎片。
_WORD_LETTERS = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_WORD_LONG = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def _check_text(text: str, field_name: str, out: list[Violation],
                brand: str | None = None, check_caps: bool = True) -> None:
    low = text.lower()

    for phrase in BANNED_PHRASES:
        # 纯 ASCII 的短语用词边界匹配，避免 "sale" 误伤 "wholesale" 这类情况
        if phrase.isascii():
            if re.search(rf"\b{re.escape(phrase)}\b", low):
                out.append(Violation(
                    field_name, "banned_phrase",
                    f"出现促销/夸大/绝对化用语「{phrase}」，平台禁止出现在文案里",
                ))
        elif phrase in low:
            out.append(Violation(
                field_name, "banned_phrase",
                f"出现促销/夸大/绝对化用语「{phrase}」，平台禁止出现在文案里",
            ))

    # ⚠️ 循环变量不能叫 brand，否则会把上面的 brand 参数覆盖掉（这个 bug 被离线自测抓到过）
    for comp_brand in COMPETITOR_BRANDS:
        if re.search(rf"\b{re.escape(comp_brand)}\b", low):
            out.append(Violation(
                field_name, "competitor_brand",
                f"提及第三方品牌「{comp_brand}」，属于商标违规风险",
            ))

    bad_chars = sorted({c for c in text if c in FORBIDDEN_CHARS})
    if bad_chars:
        out.append(Violation(
            field_name, "forbidden_char",
            f"包含平台不允许的字符：{' '.join(bad_chars)}",
        ))

    emoji = EMOJI_RE.findall(text)
    if emoji:
        out.append(Violation(field_name, "emoji", f"包含表情符号：{''.join(emoji)}"))

    if check_caps:
        allow = CAPS_ALLOWLIST | ({brand.upper()} if brand else set())
        caps = {
            w for w in re.findall(r"\b[A-Za-z]{4,}\b", text)
            if w.isupper() and w not in allow
        }
        if caps:
            out.append(Violation(
                field_name, "all_caps",
                f"出现全大写单词 {sorted(caps)}；除行业缩写外平台不允许全大写",
            ))

    if "  " in text:
        out.append(Violation(field_name, "double_space", "存在连续空格",
                             severity="warning"))


def validate(draft: ListingDraft, site: str,
             target_keywords: list[str] | None = None,
             brand: str | None = None) -> list[Violation]:
    """返回全部违规项。error 必须修，warning 建议修。"""
    rules = SITE_RULES.get(site)
    if rules is None:
        raise ValueError(f"未知站点 {site}，可选：{list(SITE_RULES)}")

    out: list[Violation] = []

    # ---- 标题 ----
    _check_text(draft.title, "title", out, brand=brand)
    if not draft.title.strip():
        out.append(Violation("title", "empty", "标题为空"))
    if len(draft.title) > rules["title_max"]:
        out.append(Violation(
            "title", "title_too_long",
            f"标题 {len(draft.title)} 字符，超出上限 {rules['title_max']}",
        ))
    if len(draft.title) < 40:
        out.append(Violation("title", "title_too_short",
                             f"标题仅 {len(draft.title)} 字符，太短会浪费搜索权重",
                             severity="warning"))

    # 关键词堆砌：同一个实词在标题里出现超过 3 次
    words = [w.lower() for w in _WORD_LONG.findall(draft.title)]
    for w in set(words):
        if words.count(w) > 3:
            out.append(Violation("title", "keyword_stuffing",
                                 f"「{w}」在标题里出现 {words.count(w)} 次，疑似堆砌关键词"))

    # ---- 五点描述 ----
    if len(draft.bullets) != rules["bullet_count"]:
        out.append(Violation(
            "bullets", "bullet_count",
            f"需要 {rules['bullet_count']} 条，实际 {len(draft.bullets)} 条",
        ))
    seen: set[str] = set()
    for i, b in enumerate(draft.bullets):
        name = f"bullets[{i + 1}]"
        _check_text(b, name, out, brand=brand)
        if len(b) > rules["bullet_max"]:
            out.append(Violation(name, "bullet_too_long",
                                 f"{len(b)} 字符，超出上限 {rules['bullet_max']}"))
        elif len(b) > rules["bullet_recommended"]:
            out.append(Violation(
                name, "bullet_too_long_recommended",
                f"{len(b)} 字符，超过建议长度 {rules['bullet_recommended']}，移动端会被截断",
                severity="warning",
            ))
        key = b.strip().lower()
        if key in seen:
            out.append(Violation(name, "duplicate_bullet", "与前面某条描述重复"))
        seen.add(key)

    # ---- 长描述 ----
    _check_text(draft.description, "description", out, brand=brand)
    if len(draft.description) > rules["description_max"]:
        out.append(Violation(
            "description", "description_too_long",
            f"{len(draft.description)} 字符，超出上限 {rules['description_max']}",
        ))

    # ---- 后台搜索词 ----
    st = draft.search_terms
    _check_text(st, "search_terms", out, brand=brand)
    nbytes = len(st.encode("utf-8"))
    if nbytes > rules["search_terms_bytes"]:
        out.append(Violation(
            "search_terms", "search_terms_too_long",
            f"{nbytes} 字节，超出上限 {rules['search_terms_bytes']} 字节",
        ))
    title_words = {w.lower() for w in _WORD_LONG.findall(draft.title)}
    repeated = sorted({w.lower() for w in _WORD_LONG.findall(st) if w.lower() in title_words})
    if repeated:
        out.append(Violation(
            "search_terms", "search_terms_repeat",
            f"这些词已在标题中出现，后台搜索词里重复它们是浪费：{repeated[:8]}",
            severity="warning",
        ))

    # ---- 关键词覆盖（这是可量化的效果指标） ----
    # 注意：用「词级覆盖」而不是「整短语原文匹配」——
    # 搜索引擎按词匹配，要求长尾短语原样出现会把好文案误判成没覆盖。
    # 详见 rules.keyword_coverage_score 里的说明。
    if target_keywords:
        body = " ".join([draft.title, *draft.bullets, draft.description]).lower()
        missing = [k for k in target_keywords if not keyword_covered(k, body)]
        if missing:
            out.append(Violation(
                "all", "keyword_missing",
                f"以下目标关键词还有实词没写进正文：{missing}",
                severity="warning",
            ))

    return out


# ---------------------------------------------------------------- 确定性兜底修复

def autocorrect(draft: ListingDraft, site: str) -> tuple[ListingDraft, list[str]]:
    """模型改不动时的兜底：用规则做确定性裁剪。

    【面试要点】线上服务不能因为「模型这轮没听话」就整个失败。
    兜底策略保证「一定有一条能用的输出」，这是工程和 Demo 的区别。
    """
    rules = SITE_RULES[site]
    notes: list[str] = []
    data = draft.model_dump()

    if len(data["title"]) > rules["title_max"]:
        data["title"] = data["title"][: rules["title_max"]].rstrip(" ,-")
        notes.append(f"标题超长，已截断到 {rules['title_max']} 字符")

    fixed_bullets = []
    for b in data["bullets"][: rules["bullet_count"]]:
        if len(b) > rules["bullet_max"]:
            b = b[: rules["bullet_max"]].rstrip(" ,-")
            notes.append("有描述超长，已截断")
        fixed_bullets.append(b)
    while len(fixed_bullets) < rules["bullet_count"]:
        fixed_bullets.append("See product images and the specification table for details.")
        notes.append("描述条数不足，已补齐占位内容（需人工补写）")
    data["bullets"] = fixed_bullets

    if len(data["description"]) > rules["description_max"]:
        data["description"] = data["description"][: rules["description_max"]].rstrip(" ,-")
        notes.append("长描述超长，已截断")

    if len(data["search_terms"].encode("utf-8")) > rules["search_terms_bytes"]:
        encoded = data["search_terms"].encode("utf-8")[: rules["search_terms_bytes"]]
        data["search_terms"] = encoded.decode("utf-8", errors="ignore").rsplit(" ", 1)[0]
        notes.append("后台搜索词超字节，已裁剪")

    return ListingDraft(**data), notes
