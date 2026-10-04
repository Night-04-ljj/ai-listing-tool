"""
批量生成：产品表进 → 可直接给运营的表格出。

用法：
    python batch.py --site US
    python batch.py --site US --site DE
    python batch.py --site US --sku S02M1225B
    python batch.py --site US --timestamp     # 保留一份带时间戳的历史文件

【为什么一定要有批量】
单个 SKU 生成得再漂亮，运营也不会用 —— 他们手上是几百个 SKU 的表。
「能不能批量跑、跑完能不能直接用」才是业务方真正关心的问题。

【产出物只有 3 类，不再是一条一个文件】
    output/all_listings.json   所有结果，键 = "SKU|站点"（覆盖式，不会重复堆积）
    output/all_listings.md     人类可读汇总
    output/listings_{站点}.csv 运营交付物（固定文件名，Excel 直接打开）
"""
from __future__ import annotations

import argparse
import shutil
from datetime import datetime

import results
from config import OUTPUT_DIR
from generate import ListingGenerator, load_products, save_result
from rules import SITE_RULES


def main() -> None:
    ap = argparse.ArgumentParser(description="批量生成 Amazon Listing")
    ap.add_argument("--site", action="append", required=True,
                    choices=list(SITE_RULES), help="可重复指定多个站点")
    ap.add_argument("--sku", action="append", help="只跑指定的 SKU，可重复")
    ap.add_argument("--split", action="store_true", help="额外导出逐条的 md + json")
    ap.add_argument("--timestamp", action="store_true",
                    help="额外留一份带时间戳的 CSV 备份（默认只保留固定文件名）")
    args = ap.parse_args()

    products = load_products()
    if args.sku:
        wanted = {s.lower() for s in args.sku}
        products = [p for p in products if p["sku"].lower() in wanted]
    if not products:
        raise SystemExit("没有匹配的产品，检查 --sku")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    total_ok = 0

    for site in args.site:
        print(f"\n{'=' * 72}\n站点 {site}：{len(products)} 个 SKU\n{'=' * 72}")
        generator = ListingGenerator(site)
        site_ok = 0

        for i, product in enumerate(products, 1):
            print(f"[{i}/{len(products)}] SKU {product['sku']} …")
            try:
                result = generator.generate(product, verbose=True)
            except Exception as e:  # noqa: BLE001
                # 单个 SKU 失败不能让整批挂掉 —— 批量任务的基本容错
                print(f"  ✗ 失败：{e}")
                continue
            # rebuild_md=False：批量时先不重排 markdown，跑完统一重建
            save_result(result, split=args.split)
            if result["passed"]:
                site_ok += 1
            print(f"  {'✓ 通过' if result['passed'] else '✗ 有必须修复项'}"
                  f"（{result['rounds']} 轮，{len(result['violations'])} 项提示）")

        # 一个站点一个固定名的 CSV
        data = results.load_all()
        csv_path = results.write_csv(data, site)
        if csv_path and args.timestamp:
            backup = csv_path.with_name(f"listings_{site}_{stamp}.csv")
            shutil.copy2(csv_path, backup)
            print(f"历史备份：{backup.name}")

        total_ok += site_ok
        print(f"\n站点 {site} 完成：{site_ok}/{len(products)} 通过合规校验")
        c = generator.client
        print(f"大模型调用：缓存命中 {c.hits} 次 / 真实调用 {c.misses} 次")
        if c.hits and not c.misses:
            print("  （本次全部命中缓存，断网也能跑 —— 现场演示靠它）")
        print(f"运营表格：{csv_path}")

    md = results.rebuild_markdown()
    data = results.load_all()
    print(f"\n{'=' * 72}")
    print(f"全部完成：这次通过 {total_ok} 条；仓库里现在共 {len(data)} 条文案")
    print(f"汇总（给人看）：{md}")
    print(f"汇总（给程序读）：{results.JSON_PATH}")
    print("   ← .json / .md / .csv 各一个，不再是每条两个文件")


if __name__ == "__main__":
    main()
