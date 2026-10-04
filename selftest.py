"""
离线自测：不调用大模型、不花一分钱，验证「规则引擎」能不能抓到问题。

用法：
    python selftest.py

【面试考点：为什么要有这个文件】
很多人做 AI 项目，一测试就得烧 API 额度，所以干脆不测。
但**规则校验是纯逻辑，完全可以离线测**。把校验逻辑的测试和模型调用解耦，
才能放心改规则、也不怕线上出事。

这段话说出来，面试官会知道你区分得清「哪些部分需要模型、哪些部分不需要」。
"""
from __future__ import annotations

import sys

from prompts import EXAMPLE
from schema import ListingDraft, validate

SITE = "US"
BRAND = "FITUEYES"


def errors(draft: ListingDraft) -> list:
    return [v for v in validate(draft, SITE, brand=BRAND) if v.severity == "error"]


def rules_hit(draft: ListingDraft) -> set[str]:
    return {v.rule for v in validate(draft, SITE, brand=BRAND)}


def base() -> ListingDraft:
    return ListingDraft(**{k: (list(v) if isinstance(v, list) else v) for k, v in EXAMPLE.items()})


def build_cases() -> list[tuple[str, ListingDraft, str | None, bool]]:
    """返回 (用例名, 草稿, 期望命中的规则名, 是否应该报错)"""
    cases: list[tuple[str, ListingDraft, str | None, bool]] = []

    # 1. 内置范例应当零错误 —— 这是回归测试，防止改规则时误伤
    cases.append(("内置范例应通过", base(), None, False))

    # 2. 标题超长
    d = base()
    d.title = d.title + " Extra words to push this title well beyond the two hundred character limit " * 3
    cases.append(("标题超长", d, "title_too_long", True))

    # 3. 出现促销词
    d = base()
    d.bullets[0] = "Best seller wall mount for your living room television setup."
    cases.append(("促销词 best seller", d, "banned_phrase", True))

    # 4. 全大写单词
    d = base()
    d.bullets[1] = "AMAZING steel frame that keeps your screen steady on the wall."
    cases.append(("全大写单词", d, "all_caps", True))

    # 5. emoji
    d = base()
    d.bullets[2] = "Low profile design that saves space 😀 in any living room."
    cases.append(("emoji", d, "emoji", True))

    # 6. 禁用字符
    d = base()
    d.title = d.title + " !!!"
    cases.append(("禁用字符 !", d, "forbidden_char", True))

    # 7. 提及第三方品牌
    d = base()
    d.description = "Works with Samsung and LG televisions in most living rooms."
    cases.append(("竞品品牌 Samsung", d, "competitor_brand", True))

    # 8. 五点描述条数不对
    d = base()
    d.bullets = d.bullets[:4]
    cases.append(("五点条数不足", d, "bullet_count", True))

    # 9. 五点描述重复
    d = base()
    d.bullets[1] = d.bullets[0]
    cases.append(("五点描述重复", d, "duplicate_bullet", True))

    # 10. 搜索词超字节
    d = base()
    d.search_terms = " ".join(["longkeyword"] * 40)
    cases.append(("搜索词超字节", d, "search_terms_too_long", True))

    # 11. 长描述超长
    d = base()
    d.description = d.description * 6
    cases.append(("长描述超长", d, "description_too_long", True))

    return cases


def main() -> int:
    cases = build_cases()
    passed = 0
    failed: list[str] = []

    print(f"运行 {len(cases)} 个离线校验用例（不调用模型）\n")
    for name, draft, expect_rule, expect_error in cases:
        errs = errors(draft)
        hit = rules_hit(draft)
        ok = (bool(errs) == expect_error) and (expect_rule is None or expect_rule in hit)
        if ok:
            passed += 1
            detail = "无报错" if not errs else f"命中 {sorted({e.rule for e in errs})}"
            print(f"  ✓ {name:<22} {detail}")
        else:
            failed.append(name)
            print(f"  ✗ {name:<22} 期望规则 {expect_rule} / 期望报错={expect_error}，"
                  f"实际命中 {sorted(hit)}")

    print(f"\n结果：{passed}/{len(cases)} 通过")
    if failed:
        print("失败用例：" + "、".join(failed))
        return 1
    print("规则引擎自测全部通过 —— 可以放心改规则，回归成本为零。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
