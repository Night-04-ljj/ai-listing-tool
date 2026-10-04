"""
Prompt 组装。

【这就是「用 Prompt 代替 RAG」的具体做法】
品牌调性、核心卖点、平台规则、目标关键词、优秀范例 —— 这些「知识」
规模不大，全部直接写进 Prompt。不需要向量库，不需要检索框架。

什么时候该换成检索？当关键词库从几十条涨到几千条、Prompt 装不下、
token 成本压不住的时候。那时我会先试「按类目过滤 + 关键词匹配」，
而不是一上来就上向量库 —— 先选最简单的能解决问题的方案。
"""
from __future__ import annotations

from rules import SITE_RULES

# ---------------------------------------------------------------- 范例

# 一条「结构参考」范例：示范信息密度和写法，不是让模型照抄语言
EXAMPLE = {
    "title": (
        "FITUEYES TV Wall Mount for 32-75 inch Flat Screen TVs, Heavy Duty Steel "
        "Bracket up to 60 kg, VESA 200x200 to 600x400, Low Profile 35 mm from Wall"
    ),
    "bullets": [
        "Universal fit: works with most 32 to 75 inch flat and curved TVs that use "
        "VESA patterns from 200x200 mm to 600x400 mm.",
        "Heavy duty cold rolled steel frame holds up to 60 kg, keeping large screens "
        "steady on the wall.",
        "Low profile design sits only 35 mm from the wall, saving space in living "
        "rooms, bedrooms and offices.",
        "Tilt range from -8 to +5 degrees helps reduce glare and keeps the screen at "
        "a comfortable angle.",
        "Installation kit includes M8 expansion bolts, TV screws, a small bubble level "
        "and cable ties.",
    ],
    "description": (
        "The FITUEYES TV wall mount is built for households that want a large screen "
        "mounted safely and neatly. The cold rolled steel bracket supports screens from "
        "32 to 75 inches and up to 60 kg, and the VESA range from 200x200 mm to "
        "600x400 mm covers most flat and curved televisions sold today. Please check "
        "your television weight and VESA hole pattern before ordering."
    ),
    "search_terms": (
        "curved tv hanger fixed slim universal hardware kit 32 75 large television "
        "support home theater living room"
    ),
}


def render_example() -> str:
    bullets = "\n".join(f"{i}. {b}" for i, b in enumerate(EXAMPLE["bullets"], 1))
    return f"""示例（英语站点，仅示范结构与信息密度，语言按目标站点重写）：

Title:
{EXAMPLE['title']}

Bullets:
{bullets}

Description:
{EXAMPLE['description']}

Search terms:
{EXAMPLE['search_terms']}

【注意】范例里没有促销词、没有全大写、没有 emoji、没有第三方品牌。
目标：让买家在前 80 个字符内就看清「这是什么、适配什么、有什么硬参数」。
"""


FEW_SHOT_EXAMPLE = render_example()


# ---------------------------------------------------------------- System

def build_system_prompt(site: str, brand_guide: dict) -> str:
    rules = SITE_RULES[site]
    brand = brand_guide.get("brand", "the brand")
    tone = brand_guide.get("tone", "专业、可信、简洁，不夸张")
    props = "；".join(brand_guide.get("value_props", []))
    style_notes = "\n".join(f"- {n}" for n in brand_guide.get("style_notes", []))
    target_len = int(rules["title_max"] * rules.get("title_target_ratio", 0.9))

    return f"""你是资深亚马逊跨境电商文案专家，负责自有品牌 {brand} 在 {site} 站点的商品详情页文案（Listing）。

【品牌调性】{tone}
【品牌核心卖点】{props}
【品牌写作要求】
{style_notes}

【目标语言】{rules['language']}
{rules['locale_hint']}
【标题写法】{rules.get('title_style', '用逗号分隔卖点组')}

【必须遵守的平台规则，违反会导致拒审或下架】
1. 标题不超过 {rules['title_max']} 个字符。具体要求：
   a. 开头放品牌名，把最重要的规格写在前 80 个字符内。
   b. **充分利用字符额度**：目标长度是 {target_len} 字符左右（上限的
      {int(rules.get('title_target_ratio', 0.9) * 100)}%）。标题写太短等于浪费搜索权重——
      但也不能为了凑长度硬塞无关词。
   c. **如果参数里给了系列名或型号，必须写进标题**（例如 "Master Serie"、"Eiffel Serie"）。
   d. **至少写一个使用场景**（如客厅、卧室、办公室、会议室），如果参数里能推断出来的话。
   e. 覆盖买家最关心的硬参数：适配尺寸、承重、VESA 孔距、颜色（参数里有的才写）。
2. 五点描述必须正好 {rules['bullet_count']} 条，每条不超过 {rules['bullet_max']} 个字符，
   建议控制在 {rules['bullet_recommended']} 个字符以内，每条开头用 2 到 4 个单词概括这条的卖点。
3. 长描述不超过 {rules['description_max']} 个字符。
4. 后台搜索词不超过 {rules['search_terms_bytes']} 字节，用空格分隔，
   不要重复标题里已经出现过的词，不要放品牌名。
5. 严禁促销与夸大用语：best seller、sale、discount、free shipping、money back、guarantee、
   top rated、number one、hot item、eco-friendly、waterproof 等。
6. 严禁全大写单词（VESA、HDMI、TV 等行业缩写除外），严禁 emoji，
   严禁使用这些字符：! ? $ _ ^ ~ < > [ ]。
7. 严禁提及任何第三方品牌名（电视品牌、竞品品牌）。
8. 【最重要】只能使用用户提供的产品参数。不要编造任何参数、认证、功能或测试数据。
   参数没给的，就不要写。

【输出格式】只输出一个 JSON 对象，不要输出解释、不要用 markdown 代码块包裹：
{{"title": "...", "bullets": ["...", "...", "...", "...", "..."], "description": "...", "search_terms": "..."}}"""


# ---------------------------------------------------------------- User

def build_user_prompt(product: dict, site: str, keywords: list[str],
                      extra_text: str = "") -> str:
    """组装用户消息。

    product   : 结构化参数（参数名 → 参数值）
    extra_text: 用户粘贴的**非结构化补充说明**（比如整段说明书原文）。
                模型更容易从原文里找到细节，所以两种都喂给它。
    """
    facts = "\n".join(f"- {k}: {v}" for k, v in product.items() if v not in (None, ""))
    if not facts:
        facts = "（没有提供结构化参数，请只依据下面的补充说明）"
    if keywords:
        kw_block = (
            "【目标关键词（尽量自然地覆盖，不要堆砌）】\n"
            + ", ".join(keywords)
        )
    else:
        # 没有词库时，绝不塞别的语言的关键词进去 —— 让模型按目标语言自己选
        kw_block = (
            "【目标关键词】\n"
            "（本类目在该站点暂时没有维护词库）请你**按目标市场的真实搜索习惯**，"
            "自行选择 8 到 12 个自然的搜索词并融进文案。\n"
            "不要生造不存在的词，也不要把其他语言的关键词直接搬过来。"
        )
    extra_block = ""
    if extra_text.strip():
        extra_block = (
            "\n【补充说明（用户粘贴的原文，可能包含上面没有的细节，也可以忽略无关内容）】\n"
            f"{extra_text.strip()}\n"
        )
    return f"""请为下面这个产品生成 {site} 站点的 Listing。

【产品参数（只能用这些，不得编造）】
{facts}
{extra_block}
{kw_block}

{FEW_SHOT_EXAMPLE}

现在请输出 JSON。"""


# ---------------------------------------------------------------- 修复

def build_repair_prompt(draft_json: str, violations_text: str, brand_guide: dict) -> str:
    """把「具体违规项」回喂给模型 —— 这是让合规率上去的关键一步。

    【设计要点】不要只说「请重新生成」，要说清楚**哪里违规了、违反哪条规则**。
    定位越精确，模型一次改对的概率越高，重试次数就越少，成本越低。
    """
    return f"""你上一版输出存在以下违规项，请**只修改这些问题**，其余内容尽量保持不变。

【违规清单】
{violations_text}

【上一版输出】
{draft_json}

【修改要求】
- 超长就压缩：优先删掉重复修饰词，保留硬参数（尺寸、承重、孔距、材质）。
- 出现禁用词就替换成客观描述，例如把夸大表述改成具体规格。
- 条数不对就补足或合并，保持 {brand_guide.get('brand', 'the brand')} 的品牌调性。
- 关键词没覆盖到的，自然地融进标题或描述，不要堆砌。

只输出修正后的完整 JSON，不要输出任何解释。"""
