"""
结果仓库：所有生成结果放在**一个文件**里，按 (SKU, 站点) 覆盖写入。

【为什么不再一条一个文件】
一条 Listing 生成一次会产生 2 个文件（json + md）。
6 个 SKU × 4 个站点 = 48 个文件，100 个 SKU × 4 站点 = 800 个文件，完全没法管理。

【为什么用「一个字典 + 原子重写」而不是「追加到 jsonl」】
追加（append）看起来很省事，但有个隐蔽的问题：
**同一个 SKU 重跑一次就会产生重复记录**。改了规则要重新生成是很常见的操作，
追加方式下这个文件会越积越多，而且你分不清哪条是最新的。
正确语义是**按 (SKU, 站点) 覆盖**。

数据量方面：一条 3KB，1000 条也才 3MB，整体重写的成本可以忽略；
换来的是「天然去重 + 读取简单 + 可以用 git diff 看变化」。

【产出物（由 24 个文件降到 3 类）】
    output/all_listings.json   所有结果，键 = "SKU|站点"
    output/all_listings.md     人类可读汇总（一个 SKU 一节）
    output/listings_{站点}.csv 运营交付物，固定文件名（Excel 可直接打开）

需要把单独一条发给同事时，用 `--split` 再生成逐条文件。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from rules import SITE_RULES

JSON_PATH = OUTPUT_DIR / "all_listings.json"
MD_PATH = OUTPUT_DIR / "all_listings.md"

_KEY_SEP = "|"


# ---------------------------------------------------------------- 基础读写

def key_of(sku: str, site: str) -> str:
    return f"{sku}{_KEY_SEP}{site}"


def split_key(key: str) -> tuple[str, str]:
    sku, _, site = key.partition(_KEY_SEP)
    return sku, site


def load_all() -> dict[str, dict]:
    """读全部结果。文件不存在就返回空字典。"""
    if not JSON_PATH.exists():
        return {}
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        print(f"警告：{JSON_PATH.name} 解析失败，已忽略（可能写入过程中被中断）")
        return {}


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免写到一半断电/中断导致文件损坏。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_all(data: dict[str, dict]) -> None:
    ordered = {k: data[k] for k in sorted(data)}
    _atomic_write(JSON_PATH, json.dumps(ordered, ensure_ascii=False, indent=2))


def upsert(result: dict, rebuild_md: bool = True) -> Path:
    """写入（或覆盖）一条结果。

    批量场景下把 rebuild_md 设为 False，跑完再统一调一次 rebuild_markdown()，
    避免每写一条就重排一次整份 markdown。
    """
    data = load_all()
    data[key_of(result["sku"], result["site"])] = result
    save_all(data)
    if rebuild_md:
        rebuild_markdown(data)
    return JSON_PATH


def get(sku: str, site: str) -> dict | None:
    return load_all().get(key_of(sku, site))


# ---------------------------------------------------------------- Markdown 汇总

def render_markdown(data: dict[str, dict]) -> str:
    if not data:
        return "# AI Listing 汇总\n\n（还没有生成任何文案）\n"

    by_sku: dict[str, list[dict]] = {}
    for _, r in sorted(data.items()):
        by_sku.setdefault(r["sku"], []).append(r)

    total = len(data)
    ok = sum(1 for r in data.values() if r.get("passed"))
    rounds = total and sum(r.get("rounds", 0) for r in data.values()) / total

    lines = [
        "# AI Listing 汇总",
        "",
        f"共 **{total}** 条文案 ｜ 覆盖 **{len(by_sku)}** 个 SKU ｜ "
        f"合规 **{ok}/{total}**（{ok / total:.0%}）｜ 平均修复轮数 **{rounds:.2f}**",
        "",
        f"最后更新：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 总览",
        "",
        "| SKU | 站点 | 合规 | 标题字符 | 修复轮数 | 违规项(必须修/建议) |",
        "|---|---|---|---|---|---|",
    ]
    for sku, rows in by_sku.items():
        for r in sorted(rows, key=lambda x: x["site"]):
            listing = r["listing"]
            errs = sum(1 for v in r.get("violations", []) if v["severity"] == "error")
            warns = len(r.get("violations", [])) - errs
            lines.append(
                f"| {sku} | {r['site']} | {'✓' if r.get('passed') else '✗'} | "
                f"{len(listing['title'])} | {r.get('rounds', '?')} | {errs}/{warns} |"
            )

    for sku, rows in by_sku.items():
        lines += ["", "---", "", f"# {sku}"]
        for r in sorted(rows, key=lambda x: x["site"]):
            site = r["site"]
            lang = SITE_RULES.get(site, {}).get("language", site)
            listing = r["listing"]
            lines += [
                "",
                f"## {sku} · {site}（{lang}）",
                "",
                f"> 模型 `{r.get('model', '?')}` ｜ 修复轮数 {r.get('rounds', '?')} ｜ "
                f"合规 {'通过' if r.get('passed') else '未通过'} ｜ "
                f"关键词来源：{r.get('keyword_source', '?')}",
                "",
                f"### Title（{len(listing['title'])} 字符）",
                "",
                listing["title"],
                "",
                "### Bullet Points",
                "",
            ]
            for i, b in enumerate(listing["bullets"], 1):
                lines.append(f"{i}. {b}")
            lines += [
                "",
                f"### Description（{len(listing['description'])} 字符）",
                "",
                listing["description"],
                "",
                f"### Search Terms（{len(listing['search_terms'].encode('utf-8'))} 字节）",
                "",
                f"`{listing['search_terms']}`",
            ]
            if r.get("violations"):
                lines += ["", "### 校验提示", ""]
                for v in r["violations"]:
                    mark = "✗" if v["severity"] == "error" else "!"
                    lines.append(f"- {mark} [{v['field']}] {v['message']}")
            if r.get("autofix_notes"):
                lines += ["", "### 兜底裁剪记录", ""]
                lines += [f"- {n}" for n in r["autofix_notes"]]
    lines.append("")
    return "\n".join(lines)


def rebuild_markdown(data: dict[str, dict] | None = None) -> Path:
    _atomic_write(MD_PATH, render_markdown(data if data is not None else load_all()))
    return MD_PATH


# ---------------------------------------------------------------- CSV 导出

def flatten(result: dict) -> dict:
    """把一条结果压平成一行，方便写 CSV。"""
    listing = result["listing"]
    row = {
        "sku": result["sku"],
        "site": result["site"],
        "title_len": len(listing["title"]),
        "title": listing["title"],
        "description_len": len(listing["description"]),
        "description": listing["description"],
        "search_terms": listing["search_terms"],
        "search_terms_bytes": len(listing["search_terms"].encode("utf-8")),
        "rounds": result.get("rounds"),
        "passed": result.get("passed"),
        "keyword_source": result.get("keyword_source", ""),
        "error_count": sum(1 for v in result.get("violations", []) if v["severity"] == "error"),
        "warning_count": sum(1 for v in result.get("violations", []) if v["severity"] == "warning"),
    }
    for i in range(5):
        key = f"bullet_{i + 1}"
        row[key] = listing["bullets"][i] if i < len(listing["bullets"]) else ""
        row[f"{key}_len"] = len(row[key])
    return row


def csv_rows(data: dict[str, dict], site: str) -> list[dict]:
    rows = [flatten(r) for _, r in sorted(data.items()) if r.get("site") == site]
    return sorted(rows, key=lambda x: x["sku"])


def csv_path(site: str) -> Path:
    """固定文件名 —— 不带时间戳，避免每次跑都新增一个文件。"""
    return OUTPUT_DIR / f"listings_{site}.csv"


def write_csv(data: dict[str, dict], site: str) -> Path | None:
    import csv as _csv

    rows = csv_rows(data, site)
    if not rows:
        return None
    path = csv_path(site)
    fieldnames: list[str] = []
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # utf-8-sig 是为了 Excel 双击打开不乱码
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_all_csvs(data: dict[str, dict] | None = None) -> list[Path]:
    data = data if data is not None else load_all()
    sites = sorted({r["site"] for r in data.values()})
    out = []
    for s in sites:
        p = write_csv(data, s)
        if p:
            out.append(p)
    return out


# ---------------------------------------------------------------- 可选：逐条导出

def write_split_files(result: dict) -> list[Path]:
    """把单独一条导出成 md + json（仅在需要单独发给别人时使用）。

    命令行加 --split，或 Web 页面点「导出这一条」时调用。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"listing_{result['sku']}_{result['site']}"
    jp = OUTPUT_DIR / f"{stem}.json"
    mp = OUTPUT_DIR / f"{stem}.md"
    jp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    listing = result["listing"]
    lines = [
        f"# {result['sku']} · {result['site']} Listing",
        "",
        f"- 模型：`{result.get('model', '?')}`｜修复轮数：{result.get('rounds')}"
        f"｜合规：{'通过' if result.get('passed') else '未通过'}",
        "",
        "## Title",
        "",
        listing["title"],
        "",
        f"（{len(listing['title'])} 字符）",
        "",
        "## Bullet Points",
        "",
    ]
    for i, b in enumerate(listing["bullets"], 1):
        lines += [f"**{i}.** {b}", ""]
    lines += [
        "## Description",
        "",
        listing["description"],
        "",
        "## Search Terms",
        "",
        f"`{listing['search_terms']}`",
        "",
    ]
    mp.write_text("\n".join(lines), encoding="utf-8")
    return [mp, jp]


# ---------------------------------------------------------------- 维护命令

def legacy_files() -> list[Path]:
    """旧版留下的一条一个文件（listing_xxx_SITE.json / .md）。"""
    if not OUTPUT_DIR.exists():
        return []
    return sorted(
        p for p in OUTPUT_DIR.iterdir()
        if p.is_file() and p.name.startswith("listing_") and p.suffix in {".json", ".md"}
    )


def backups() -> list[Path]:
    """所有历史备份（all_listings.backup_*.json），按时间倒序。"""
    if not OUTPUT_DIR.exists():
        return []
    return sorted(OUTPUT_DIR.glob("all_listings.backup_*.json"), reverse=True)


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="结果仓库维护")
    ap.add_argument("--stats", action="store_true", help="看仓库里有多少条")
    ap.add_argument("--clean-old", action="store_true",
                    help="把旧版「一条一个文件」删掉（数据已经在 all_listings.json 里）")
    ap.add_argument("--rebuild", action="store_true", help="重新生成 all_listings.md 和 CSV")
    ap.add_argument("--clear", action="store_true",
                    help="清空结果仓库（会先备份成 all_listings.backup_<时间>.json）")
    ap.add_argument("--list-backups", action="store_true", help="列出所有历史备份")
    ap.add_argument("--restore-backup", metavar="文件", nargs="?", const="__latest__",
                    help="从备份恢复结果（不填文件名则恢复最新的一份）")
    ap.add_argument("--merge-backup", metavar="文件", nargs="?", const="__latest__",
                    help="把备份**合并**进当前仓库（保留现有结果，同键以备份为准）")
    args = ap.parse_args()

    if args.list_backups:
        bs = backups()
        if not bs:
            print("没有历史备份")
        else:
            for b in bs:
                data = {}
                try:
                    data = json.loads(b.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
                skus = sorted({v.get("sku", "?") for v in data.values()})
                print(f"  {b.name}   {len(data)} 条   SKU: {skus[:8]}")

    if args.clear:
        import shutil
        from datetime import datetime

        if not JSON_PATH.exists():
            print("结果仓库本来就是空的")
        else:
            data = load_all()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = OUTPUT_DIR / f"all_listings.backup_{stamp}.json"
            shutil.copy2(JSON_PATH, backup)
            save_all({})
            rebuild_markdown({})
            by_site: dict[str, int] = {}
            for r in data.values():
                by_site[r["site"]] = by_site.get(r["site"], 0) + 1
            print(f"已清空 {len(data)} 条结果：")
            print(f"  涉及的 SKU：{sorted({r.get('sku', '?') for r in data.values()})}")
            print(f"  按站点     ：{by_site}")
            print(f"  备份文件   ：{backup.name}")
            print()
            print("⚠️ 如果清错了，用这条命令恢复：")
            print(f'      py results.py --restore-backup {backup.name}')
            print("   （或直接 py results.py --restore-backup 恢复最新的一份）")
            print("提示：换了产品表之后，记得重新跑一次 batch.py 或网页上的批量生成。")

    if args.restore_backup is not None or args.merge_backup is not None:
        import shutil
        from datetime import datetime

        which = args.restore_backup if args.restore_backup is not None else args.merge_backup
        merge = args.merge_backup is not None
        bs = backups()
        if not bs:
            print("没有历史备份可恢复")
            return
        target = bs[0] if which == "__latest__" else OUTPUT_DIR / which
        if not target.exists():
            print(f"找不到备份文件：{target}")
            return
        old = json.loads(target.read_text(encoding="utf-8"))
        current = load_all()

        if merge:
            current.update(old)
            merged = current
        else:
            # 覆盖前先把当前状态也存一份，免得恢复错了又回不去
            if current:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(JSON_PATH, OUTPUT_DIR / f"all_listings.backup_{stamp}.json")
            merged = old

        save_all(merged)
        rebuild_markdown(merged)
        print(f"已从 {target.name} {'合并' if merge else '恢复'}："
              f"{len(old)} 条 -> 现在共 {len(merged)} 条")
        for p in write_all_csvs(merged):
            print(f"重建 CSV：{p.name}")

    if args.clean_old:
        old = legacy_files()
        if not old:
            print("没有旧版文件，无需清理")
        else:
            print(f"将删除 {len(old)} 个旧文件（数据已保存在 {JSON_PATH.name} 里）：")
            for p in old:
                print(f"  - {p.name}")
                p.unlink()
            print("清理完成")

    if args.rebuild:
        data = load_all()
        print(f"重建 markdown：{rebuild_markdown(data)}")
        for p in write_all_csvs(data):
            print(f"重建 CSV：{p}")

    if args.stats or not any([args.clean_old, args.rebuild, args.clear,
                              args.list_backups, args.restore_backup is not None,
                              args.merge_backup is not None]):
        data = load_all()
        by_site: dict[str, int] = {}
        for r in data.values():
            by_site[r["site"]] = by_site.get(r["site"], 0) + 1
        ok = sum(1 for r in data.values() if r.get("passed"))
        print(f"结果仓库：{JSON_PATH}")
        print(f"  总条数   : {len(data)}")
        print(f"  合规     : {ok}/{len(data)}")
        print(f"  按站点   : {by_site}")
        print(f"  旧的散文件: {len(legacy_files())} 个")


if __name__ == "__main__":
    _cli()
