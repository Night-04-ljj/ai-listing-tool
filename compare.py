"""
对比：AI 生成的 Listing 标题 vs 真实在售的 Listing 标题。

用法：
    python compare.py                 # 用 data/benchmark.json 里的真实标题做基准
    python compare.py --report output/compare_report.md

【为什么这件事有价值】
合规率只能说明"没违规"，说明不了"写得好不好"。
而**真实在售的标题就是 ground truth**：它是运营真金白银在跑的东西，
标题里的信息顺序、卖点选择、措辞风格，都是被市场检验过的。

拿 AI 输出和它做结构化对比，就能回答三个具体问题：
    1. 该有的信息（品牌、尺寸、承重、VESA、颜色）有没有漏？
    2. 信息顺序对不对？（真实标题通常品牌打头、硬参数往前放）
    3. 目标市场的表达习惯像不像？（比如德语标题爱用 | 分隔卖点）

⚠️ 诚实说明：亚马逊对爬虫拦截很严，**正文（五点描述）抓不到**，只能拿到标题。
   所以这里只对比标题。但标题恰恰是权重最高、最能反映风格的那一段。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import results
from config import DATA_DIR, OUTPUT_DIR
from rules import SITE_RULES, keyword_words

BENCHMARK_PATH = DATA_DIR / "benchmark.json"

# 各语言里的颜色词，用来判断标题有没有交代颜色
COLOR_WORDS = {
    "black", "white", "grey", "gray", "silver", "weiß", "weiss", "schwarz",
    "grau", "rosa", "pink", "白", "黒", "黑", "白い", "黒い",
}


def load_benchmark() -> list[dict]:
    if not BENCHMARK_PATH.exists():
        raise SystemExit(f"没有找到基准文件：{BENCHMARK_PATH}")
    data = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    return data.get("items", [])


def structure(title: str) -> dict:
    """把标题的结构特征抽出来，用于对比。"""
    low = title.lower()
    brand = (title.split()[0].upper() == "FITUEYES") if title.strip() else False
    return {
        "长度": len(title),
        "品牌前置": brand,
        "含尺寸": bool(re.search(r"\d{2}\s*[-–~至]\s*\d{2}", title))
                  or bool(re.search(r"\d{2}\s*(inch|zoll|zoll|型|寸)", low)),
        "含承重": bool(re.search(r"\d+\s*(kg|kilogramm|千克)", low)),
        "含VESA": "vesa" in low,
        "含颜色": any(w in low.split() or w in low for w in COLOR_WORDS),
        "分段数": len(re.findall(r"[|,，、]", title)) + 1,
    }


def _normalize(text: str) -> str:
    """统一数字和单位的写法，避免把 '35 kg' 和 '35kg' 当成两个不同的词。

    不做这一步的话，覆盖率会被这种纯格式差异压低好几个百分点——
    那是**度量误差**，不是文案质量问题。
    """
    t = text.lower()
    t = re.sub(r"(\d)\s+(mm|cm|kg|inch|inches|zoll|zoll)\b", r"\1\2", t)
    t = re.sub(r"(\d)\s*[*×x]\s*(\d)", r"\1x\2", t)
    return t


def word_coverage(real: str, ai: str) -> tuple[float, list[str]]:
    """真实标题里的实词，有多少出现在 AI 标题里。返回 (覆盖率, 漏掉的词)。"""
    real_words = keyword_words(_normalize(real))
    if not real_words:
        return 1.0, []
    ai_low = _normalize(ai)
    uniq = sorted(set(real_words))
    miss_uniq = sorted({w for w in uniq if w not in ai_low})
    return (len(uniq) - len(miss_uniq)) / len(uniq), miss_uniq


def compare_one(real_title: str, ai_title: str, site: str) -> dict:
    rs, as_ = structure(real_title), structure(ai_title)
    cov, missing = word_coverage(real_title, ai_title)
    limit = SITE_RULES.get(site, {}).get("title_max", 200)

    items = []
    for key in ("品牌前置", "含尺寸", "含承重", "含VESA", "含颜色"):
        if rs[key] and not as_[key]:
            items.append(f"真实标题有「{key}」，AI 标题没有")
        elif as_[key] and not rs[key]:
            items.append(f"AI 标题多了「{key}」")
    return {
        "真实长度": rs["长度"],
        "AI长度": as_["长度"],
        "长度上限": limit,
        "AI是否超限": as_["长度"] > limit,
        "真实分段": rs["分段数"],
        "AI分段": as_["分段数"],
        "实词覆盖率": cov,
        "漏掉的实词": missing,
        "结构差异": items,
    }


def build_report(pairs: list[dict]) -> str:
    lines = [
        "# AI 生成 vs 真实在售 —— 标题对比报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "> **真实标题 = ground truth。** 它是运营真金白银在跑的文案，",
        "> 信息顺序、卖点取舍、措辞风格都被市场检验过。",
        "> 亚马逊正文（五点描述）抓不到，所以这里只对比标题——但标题恰恰是权重最高的那一段。",
        "",
        "## 总览",
        "",
        "| SKU | 站点 | 真实长度 | AI长度 | 实词覆盖率 | 漏掉的实词 | 结构差异 |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in pairs:
        c = p["cmp"]
        miss = "、".join(c["漏掉的实词"][:6]) or "无"
        diff = "；".join(c["结构差异"]) or "无"
        lines.append(
            f"| {p['sku']} | {p['site']} | {c['真实长度']} | "
            f"{c['AI长度']}{' ⚠️超限' if c['AI是否超限'] else ''} | "
            f"{c['实词覆盖率']:.0%} | {miss} | {diff} |"
        )

    lines += ["", "## 逐条对照", ""]
    for p in pairs:
        c = p["cmp"]
        lines += [
            f"### {p['sku']} · {p['site']}（基准：{p['marketplace']}）",
            "",
            f"**真实在售标题**（{c['真实长度']} 字符，{c['真实分段']} 段）",
            "",
            f"> {p['real']}",
            "",
            f"**AI 生成标题**（{c['AI长度']} 字符，{c['AI分段']} 段）",
            "",
            f"> {p['ai']}",
            "",
            f"- 实词覆盖率：**{c['实词覆盖率']:.0%}**",
        ]
        if c["漏掉的实词"]:
            lines.append(f"- AI 没写到真实标题里的这些词：{c['漏掉的实词']}")
        if c["结构差异"]:
            for d in c["结构差异"]:
                lines.append(f"- {d}")
        lines += ["", f"来源：{p['source']}", ""]

    lines += [
        "---",
        "",
        "## 怎么用这份报告",
        "",
        "**不要念数字，要讲结论和下一步**：",
        "",
        "- 「我拿 AI 生成的标题和你们真实在售的标题做了对比。实词覆盖率 X%——",
        "  说明大方向对了，但漏掉了 A、B 这两个卖点。」",
        "- 「真实标题用了 `|` 分隔卖点，AI 输出的是逗号。**这是目标市场的表达习惯问题**，",
        "  可以写进 Prompt 的站点规范里。」",
        "- 「真实标题把承重写在靠前的位置，说明这是买家最关心的参数之一；",
        "  我现在的 Prompt 没有规定信息的优先级顺序，可以补上。」",
        "",
        "> ⚠️ **一个必须说清楚的局限**：只对比了 1 条真实标题，",
        "> 单个样本说明不了市场规律。要得出结论至少要 10-20 条。",
        "> 主动说出这一点，比假装有统计意义要安全得多。",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 标题 vs 真实在售标题")
    ap.add_argument("--report", default=str(OUTPUT_DIR / "compare_report.md"))
    args = ap.parse_args()

    items = load_benchmark()
    store = results.load_all()

    pairs, skipped = [], []
    for it in items:
        key = f"{it['sku']}|{it['site']}"
        got = store.get(key)
        if not got:
            skipped.append((it["sku"], it["site"]))
            continue
        ai_title = got["listing"]["title"]
        pairs.append({
            "sku": it["sku"],
            "site": it["site"],
            "marketplace": it.get("marketplace", ""),
            "source": it.get("source") or it.get("asin", ""),
            "real": it["title"],
            "ai": ai_title,
            "cmp": compare_one(it["title"], ai_title, it["site"]),
        })

    if not pairs:
        print("还没有可对比的结果。先跑批量生成：")
        for sku, site in skipped:
            print(f"  python generate.py --sku {sku} --site {site}")
        return

    print(f"对比 {len(pairs)} 组\n")
    for p in pairs:
        c = p["cmp"]
        print("=" * 72)
        print(f"{p['sku']} · {p['site']}（基准 {p['marketplace']}）")
        print("=" * 72)
        print(f"  真实（{c['真实长度']} 字符 / {c['真实分段']} 段）：")
        print(f"    {p['real']}")
        print(f"  AI  （{c['AI长度']} 字符 / {c['AI分段']} 段）：")
        print(f"    {p['ai']}")
        print(f"  实词覆盖率：{c['实词覆盖率']:.0%}"
              + (f"　漏掉：{c['漏掉的实词']}" if c["漏掉的实词"] else ""))
        for d in c["结构差异"]:
            print(f"  ⚠ {d}")
        print()

    if skipped:
        print(f"（有 {len(skipped)} 组还没生成：{skipped}）")

    Path(args.report).write_text(build_report(pairs), encoding="utf-8")
    print(f"报告已写入：{args.report}   ← 这个文件可以直接打开查看")


if __name__ == "__main__":
    main()
