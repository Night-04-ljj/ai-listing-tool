"""
评测：把生成结果变成可汇报的数字。

用法：
    python batch.py --site US          # 先生成（会调用模型）
    python evaluate.py                 # 再评测（不花钱，只读已有结果）

【这就是项目里的「量化结果」从哪来】
不要只说「我做了个生成 Listing 的工具」，要说：
    「我用 30 个 SKU × 2 个站点跑了一遍，首轮合规率 X%，
      加了一轮定点修复后到 Y%，平均修复轮数 1.2 轮，
      目标关键词覆盖率从 A% 提到 B%。」

这些数字全部来自这个文件。**有数字的项目才算项目。**
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime

from config import OUTPUT_DIR
from rules import SITE_RULES, coverage_of, keyword_covered
import results


def load_results() -> list[dict]:
    """直接从结果仓库读（一个文件），不再扫一堆 listing_*.json。"""
    data = results.load_all()
    if not data:
        raise SystemExit("还没有生成任何结果。请先运行：python batch.py --site US")
    return list(data.values())


def coverage(result: dict) -> float:
    """关键词覆盖度（词级）。

    ⚠️ 这里踩过一个坑：最早我用「整短语原文匹配」，覆盖率只有 12%，
    看起来像文案很差，其实是**指标定错了** —— 搜索引擎按词匹配，
    不要求长尾短语原样出现。改成词级覆盖后才是真实水平。
    详见 rules.keyword_coverage_score 的注释。
    """
    listing = result["listing"]
    kws = result.get("keywords") or []
    return coverage_of(
        kws, [listing["title"], *listing["bullets"], listing["description"]]
    )


def fully_covered_ratio(result: dict) -> float:
    """有多少比例的关键词做到了「所有实词都出现」。"""
    listing = result["listing"]
    kws = result.get("keywords") or []
    if not kws:
        return 1.0
    body = " ".join([listing["title"], *listing["bullets"], listing["description"]]).lower()
    return sum(1 for k in kws if keyword_covered(k, body)) / len(kws)


def score(results: list[dict]) -> dict:
    by_site: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_site[r["site"]].append(r)

    summary = {"total": len(results), "sites": {}}
    for site, rows in by_site.items():
        rules = SITE_RULES.get(site)
        n = len(rows) or 1
        length_ok = 0
        search_ok = 0
        for r in rows:
            listing = r["listing"]
            if rules is None:
                continue
            if (len(listing["title"]) <= rules["title_max"]
                    and all(len(b) <= rules["bullet_max"] for b in listing["bullets"])
                    and len(listing["description"]) <= rules["description_max"]):
                length_ok += 1
            if len(listing["search_terms"].encode("utf-8")) <= rules["search_terms_bytes"]:
                search_ok += 1

        banned = Counter()
        for r in rows:
            for v in r["violations"]:
                if v["rule"] == "banned_phrase":
                    banned[v["message"]] += 1

        summary["sites"][site] = {
            "n": len(rows),
            "pass_rate": sum(1 for r in rows if r["passed"]) / n,
            "avg_rounds": sum(r["rounds"] for r in rows) / n,
            "first_round_pass": sum(1 for r in rows if r["rounds"] == 1 and r["passed"]) / n,
            "keyword_coverage": sum(coverage(r) for r in rows) / n,
            "keyword_fully_covered": sum(fully_covered_ratio(r) for r in rows) / n,
            "length_compliance": length_ok / n,
            "search_terms_compliance": search_ok / n,
            "avg_title_len": sum(len(r["listing"]["title"]) for r in rows) / n,
            "autofix_used": sum(1 for r in rows if r.get("autofix_notes")) / n,
            "banned_left": sum(banned.values()),
            "warning_per_item": sum(
                sum(1 for v in r["violations"] if v["severity"] == "warning") for r in rows
            ) / n,
        }
    return summary


def build_report(summary: dict) -> str:
    lines = [
        "# AI Listing 工具 · 评测报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"样本总数：{summary['total']} 条 Listing",
        "",
    ]
    for site, s in summary["sites"].items():
        lines += [
            f"## {site} 站点（{s['n']} 条）",
            "",
            "| 指标 | 数值 | 说明 |",
            "|---|---|---|",
            f"| 最终合规率 | **{s['pass_rate']:.1%}** | 交付时无「必须修复项」的比例 |",
            f"| 首轮即通过率 | {s['first_round_pass']:.1%} | 不经修复就合规，越高越省钱 |",
            f"| 平均修复轮数 | {s['avg_rounds']:.2f} | 越接近 1 越好 |",
            f"| 长度合规率 | {s['length_compliance']:.1%} | 标题/五点/描述均在字符上限内 |",
            f"| 搜索词字节合规率 | {s['search_terms_compliance']:.1%} | 后台搜索词 ≤250 字节 |",
            f"| 目标关键词覆盖率 | {s['keyword_coverage']:.1%} | **词级覆盖**：关键词里的实词出现在正文中的比例 |",
            f"| 关键词全词命中率 | {s['keyword_fully_covered']:.1%} | 关键词的所有实词都写进去了的比例 |",
            f"| 平均标题长度 | {s['avg_title_len']:.0f} 字符 | 参考值，过长过短都不好 |",
            f"| 兜底裁剪使用率 | {s['autofix_used']:.1%} | 模型两轮没改好、靠规则兜底的比例 |",
            f"| 残留禁用词 | {s['banned_left']} | **必须为 0** |",
            f"| 平均建议项 | {s['warning_per_item']:.1f} | 待优化项，可人工判断 |",
            "",
        ]

    lines += [
        "## 怎么用这份报告",
        "",
        "汇报时不要念数字，要讲**因果**：",
        "",
        "- 「首轮通过率只有 X%，所以我加了定点修复，把具体违规项回喂给模型，最终合规率到 Y%。」",
        "- 「平均修复轮数是 Z，每多一轮就多一次 API 调用，所以我压缩了 Prompt 里的规则表述，",
        "  让模型第一轮就更接近合规，成本降了约 N%。」",
        "- 「残留禁用词是 0，这条是硬指标，一旦不为 0 说明校验规则有漏，属于事故。」",
        "",
        "### ⚠️ 一个必须讲出来的教训：我一开始把指标定错了",
        "",
        "最早我算关键词覆盖率用的是**整短语原文匹配** —— 把 ",
        "`heavy duty tv wall mount` 当成一个字符串去找。结果覆盖率只有 **12%**，",
        "看起来像文案质量很差。",
        "",
        "但我把文案摊开一看：heavy、duty、wall、mount 这些词**全都写进去了**，",
        "标题里就有 `Heavy Duty ... Bracket` 和 `TV Wall Mount`。",
        "**问题不在文案，在我的指标** —— 搜索引擎是按词（含词干）匹配的，",
        "不会要求你原样输出整个长尾短语。",
        "",
        "改成词级覆盖之后，覆盖率是现在的数字。",
        "这件事我记了很久：**指标定错了，你会朝着错误的方向优化。**",
        "",
        "## 已知不足（主动说出来，比被问出来强）",
        "",
        "- 文案质量（说服力、可读性）**没有客观指标**，目前只能人工抽检打分。",
        "  要量化的话需要找运营盲评，或者用 LLM-as-a-Judge 做相对排序。",
        "- 关键词覆盖率只判断「实词是否出现」，判断不了「是否自然、是否堆砌、语义是否准确」。",
        "- 平台规则会变，`rules.py` 里的规则需要定期按卖家后台核对。",
        "- 没有接 A+ 页面、图片文案、广告文案；这些是天然的下一个迭代方向。",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="评测生成质量")
    ap.add_argument("--out", default="", help="报告输出路径")
    args = ap.parse_args()

    results = load_results()
    summary = score(results)

    print(f"读取到 {len(results)} 条生成结果\n")
    for site, s in summary["sites"].items():
        print(f"[{site}] {s['n']} 条")
        print(f"  最终合规率        : {s['pass_rate']:.1%}")
        print(f"  首轮即通过率      : {s['first_round_pass']:.1%}")
        print(f"  平均修复轮数      : {s['avg_rounds']:.2f}")
        print(f"  长度合规率        : {s['length_compliance']:.1%}")
        print(f"  搜索词字节合规率  : {s['search_terms_compliance']:.1%}")
        print(f"  目标关键词覆盖率  : {s['keyword_coverage']:.1%}   ← 词级覆盖")
        print(f"  关键词全词命中率  : {s['keyword_fully_covered']:.1%}")
        print(f"  残留禁用词        : {s['banned_left']}")
        print()

    path = args.out or str(OUTPUT_DIR / "eval_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_report(summary))
    print(f"报告已写入：{path}   ← 可以直接放进 README 长期归档")


if __name__ == "__main__":
    main()
