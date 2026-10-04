"""
核心：生成 → 校验 → 定点修复 → 兜底裁剪。

用法：
    python generate.py --sku FT48-B --site US
    python generate.py --sku SD01 --site DE
    python generate.py --list                 # 看有哪些 SKU

【这个流程为什么重要】
直接让模型写文案，10 条里大概有 2 到 4 条会因为超字符、出现促销词、条数不对而不可用。
人工检查等于没省时间。
所以我做的是「闭环」：生成 → 用代码校验平台规则 → 把**具体违规项**回喂给模型重写 →
再校验，最多 2 轮；两轮还不过就用确定性规则兜底裁剪，保证一定有一条能交付的结果。

这就是「Demo」和「能上线的小工具」之间的差别。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import (
    AUTO_FIX_FALLBACK,
    CACHE_DIR,
    CACHE_LLM_RESPONSES,
    DATA_DIR,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    MAX_REPAIR_ROUNDS,
    OUTPUT_DIR,
)
from prompts import build_repair_prompt, build_system_prompt, build_user_prompt
from rules import SITE_RULES, keyword_source, keywords_for, load_brand_guide
from schema import ListingDraft, SchemaError, autocorrect, has_errors, validate
import results

PRODUCTS_PATH = DATA_DIR / "products.csv"

# 哪些字段要写进 Prompt（其余的字段是内部用的）
FACT_LABELS = {
    "brand": "品牌",
    "model_name": "型号",
    "product_type": "产品类型",
    "tv_size_range": "适配电视尺寸",
    "max_load_kg": "最大承重(kg)",
    "vesa_range": "VESA 孔距范围",
    "material": "材质",
    "color": "颜色",
    "mount_type": "安装方式",
    "wall_types": "适配墙面",
    "features": "主要功能",
    "package_includes": "包装清单",
}


# ---------------------------------------------------------------- 数据读取

def load_products() -> list[dict]:
    if not PRODUCTS_PATH.exists():
        raise FileNotFoundError(f"缺少产品表：{PRODUCTS_PATH}")
    with PRODUCTS_PATH.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_product(sku: str) -> dict:
    for row in load_products():
        if row.get("sku", "").strip().lower() == sku.strip().lower():
            return row
    raise SystemExit(f"没找到 SKU「{sku}」。用 --list 看可用的 SKU。")


def product_facts(row: dict) -> dict:
    """把一行产品数据变成「参数名 → 参数值」，直接进 Prompt。

    【为什么要容错，而不是只认固定列】
    一开始我只认 FACT_LABELS 里那几个列名。但真实场景里，
    运营给的产品表列名千奇百怪（中文表头、多了几列、少了 VESA 列……）。
    只认固定列的话，导入一张真实的产品表会得到**空参数**，
    然后模型就会开始编 —— 而且不报错。
    所以现在的规则是：
      · 内部用的列（sku / category / target_keywords）不放进 Prompt
      · 认识的中文映射成中文标签，不认识的**原样保留**
      · 空值跳过
    这样任何一张产品表都能直接用。
    """
    facts: dict[str, str] = {}
    for k, v in row.items():
        if not k or k in INTERNAL_FIELDS:
            continue
        if v is None or str(v).strip() == "":
            continue
        facts[FACT_LABELS.get(k, k)] = str(v).strip()
    return facts


# 只给程序看的列，不写进 Prompt
INTERNAL_FIELDS = {"sku", "category", "target_keywords"}

# 解析用户粘贴内容时可用的分隔符（中英文冒号、等号、竖线、制表符）
_SEPARATORS = ("：", ":", "=", "|", "\t")


def parse_product_text(text: str) -> tuple[dict, str]:
    """把用户粘贴的一段文字解析成 (结构化参数, 剩余自由文本)。

    【为什么要两种都支持】
    运营手上的资料是两种形态混着的：
        · 规格表里的键值对 —— 「承重: 60kg」
        · 说明书里的一整段话 —— 「适用于 32 到 75 英寸电视，安装前请确认墙体……」
    所以这个函数**混着解析**：能认出键值的行变成结构参数，
    认不出的行原样进「补充说明」，两种都喂给模型。

    判定「这是键值对」的条件比较保守（键要短、没有空格、不是网址），
    宁可把它当成自由文本，也不要错误地拆出一堆乱七八糟的参数名。
    """
    facts: dict[str, str] = {}
    rest: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•·").strip()
        if not line:
            continue
        parsed = False
        for sep in _SEPARATORS:
            if sep not in line:
                continue
            k, _, v = line.partition(sep)
            k, v = k.strip(), v.strip()
            if k and v and len(k) <= 24 and " " not in k and "/" not in k:
                # 认识的产品表列名（英文）映射成中文标签，其余原样保留
                facts[FACT_LABELS.get(k) or FACT_LABELS.get(k.lower()) or k] = v
                parsed = True
                break
        if not parsed:
            rest.append(line)
    return facts, "\n".join(rest)


# ---------------------------------------------------------------- 模型调用

def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。

    【别小看这个函数】模型经常给 JSON 加一句解释、或者用 ```json 包起来。
    直接 json.loads 会崩。生产代码必须容错。
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"模型输出里没有 JSON：{text[:200]}")
    return json.loads(text[start : end + 1])


class _BadRequest(Exception):
    """HTTP 400/422 —— 多半是某个可选参数不被这个接口支持，值得降级重试一次。"""

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"HTTP {code}：{detail}")
        self.code = code
        self.detail = detail


class LLMClient:
    """带缓存的大模型客户端 —— **只用 Python 标准库发 HTTP 请求**。

    【为什么不用 openai SDK】
    为了调一个 HTTP 接口，SDK 会带进 10 多个传递依赖（httpx / anyio / sniffio / jiter …）。
    依赖越多，`pip install` 卡住或失败的几率就越大。而这个项目只需要一个
    POST + 解析 JSON，标准库的 urllib 完全够用。

    直接的好处：
      1) 只需安装 pydantic + python-dotenv 两个包，装依赖几乎不会失败；
      2) 报错信息由我自己控制，401 就明说「API Key 不对」，比 SDK 的堆栈友好得多；
      3) 这样设计的好处是「我不依赖 SDK，因为我知道这个接口长什么样」——
         这句话证明你理解 HTTP 层，而不只是会调包。

    【为什么要缓存】
    1. 改完规则重跑评测时，同样的 Prompt 不该重复付费；
    2. **线下现场演示时，断网也能把完整流程跑一遍** —— 这条在实践中救过很多人。
    """

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0

    # ---------------- 缓存 ----------------
    def _cache_path(self, messages: list[dict]) -> Path:
        payload = json.dumps(
            {"model": LLM_MODEL, "temperature": LLM_TEMPERATURE, "messages": messages},
            ensure_ascii=False, sort_keys=True,
        )
        return CACHE_DIR / f"{hashlib.sha1(payload.encode('utf-8')).hexdigest()}.json"

    def chat_json(self, messages: list[dict]) -> dict:
        cache_file = self._cache_path(messages) if CACHE_LLM_RESPONSES else None
        if cache_file and cache_file.exists():
            self.hits += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))

        result = self._call_api(messages)
        self.misses += 1

        if cache_file:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    # ---------------- HTTP ----------------
    @staticmethod
    def _endpoint() -> str:
        base = (LLM_BASE_URL or "https://api.deepseek.com/v1").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _post(self, payload: dict) -> str:
        if not LLM_API_KEY:
            raise SystemExit(
                "LLM_API_KEY 为空。\n"
                "  请在 ai-listing-tool/.env 里填上你的 key（第 8 项菜单可以打开它）。"
            )
        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (401, 403):
                raise RuntimeError(
                    f"API Key 被拒绝（HTTP {e.code}）。\n"
                    f"  检查 .env 里的 LLM_API_KEY：不要在等号后加引号或空格。\n"
                    f"  服务端返回：{detail}"
                ) from e
            if e.code == 402:
                raise RuntimeError(
                    f"账户余额不足（HTTP 402）。服务端返回：{detail}"
                ) from e
            if e.code == 404:
                raise RuntimeError(
                    f"接口地址不对（HTTP 404）：{self._endpoint()}\n"
                    f"  检查 .env 里的 LLM_BASE_URL，确认结尾带 /v1。"
                ) from e
            raise _BadRequest(e.code, detail) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"连不上模型服务：{e.reason}\n"
                "  可能没网，或者现场网络受限。如果你的缓存已经生成过，"
                "重跑同样的内容是命中缓存、不需要联网的。"
            ) from e

        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"模型返回的结构不对：{str(body)[:300]}") from e

    def _call_api(self, messages: list[dict]) -> dict:
        last_err: Exception | None = None
        json_mode = True  # 先试 JSON 模式，不支持就退回普通模式

        for attempt in range(4):
            payload: dict = {
                "model": LLM_MODEL,
                "messages": messages,
                "temperature": LLM_TEMPERATURE,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            try:
                return _extract_json(self._post(payload))
            except _BadRequest as e:
                if json_mode:
                    # 很多 OpenAI 兼容接口不支持 response_format，退回普通模式，不计入重试
                    json_mode = False
                    continue
                last_err = e
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "API Key" in str(e) or "余额不足" in str(e) or "接口地址不对" in str(e):
                    raise  # 配置类错误重试也没用，直接报出来
                time.sleep(1.5 * (attempt + 1))

        raise RuntimeError(f"模型调用失败（已重试）：{last_err}") from last_err


# ---------------------------------------------------------------- 生成主流程

class ListingGenerator:
    def __init__(self, site: str) -> None:
        if site not in SITE_RULES:
            raise SystemExit(f"未知站点 {site}，可选：{list(SITE_RULES)}")
        self.site = site
        self.brand_guide = load_brand_guide()
        self.brand = self.brand_guide.get("brand", "")
        self.client = LLMClient()

    def generate(self, row: dict, verbose: bool = True) -> dict:
        """从产品表的一行生成（命令行 / 批量走这条路）。"""
        category = row.get("category", "")
        keywords = keywords_for(category, self.site)
        kw_source = keyword_source(category, self.site)
        override = (row.get("target_keywords") or "").strip()
        if override:
            got = [k.strip() for k in override.split(";") if k.strip()]
            if got:
                keywords, kw_source = got, "产品表里手填的 target_keywords"
        return self.generate_from_facts(
            facts=product_facts(row),
            sku=row.get("sku") or "未命名",
            keywords=keywords,
            kw_source=kw_source,
            verbose=verbose,
        )

    def generate_from_facts(self, facts: dict, sku: str = "临时产品",
                            keywords: list[str] | None = None,
                            kw_source: str = "", extra_text: str = "",
                            verbose: bool = True) -> dict:
        """从任意参数字典生成（Web 上的「临时产品」走这条路）。

        【为什么单独抽出来】
        产品表的一行和用户手打的一段参数，本质是同一件事：
        一组「参数名 → 参数值」。抽出来之后两条路复用同一套
        生成 → 校验 → 定点修复 → 兜底 的闭环，不用维护两份逻辑。
        """
        keywords = keywords or []
        kw_source = kw_source or "未提供"
        messages = [
            {"role": "system", "content": build_system_prompt(self.site, self.brand_guide)},
            {"role": "user", "content": build_user_prompt(
                facts, self.site, keywords, extra_text=extra_text)},
        ]

        draft: ListingDraft | None = None
        violations = []
        rounds = 0
        autofix_notes: list[str] = []

        while rounds <= MAX_REPAIR_ROUNDS:
            rounds += 1
            if verbose:
                print(f"  第 {rounds} 轮生成中……", end="", flush=True)

            try:
                raw = self.client.chat_json(messages)
                draft = ListingDraft(**raw)
            except (SchemaError, ValueError) as e:
                if verbose:
                    print(f" 结构不符合要求（{e}），准备修复")
                messages.append({
                    "role": "user",
                    "content": (
                        "你上一版的输出不符合要求的 JSON 结构。"
                        "必须包含 title(字符串)、bullets(5 个字符串的数组)、"
                        "description(字符串)、search_terms(字符串)。请重新输出。"
                    ),
                })
                violations = []
                continue

            violations = validate(draft, self.site, keywords, brand=self.brand)
            errors = [v for v in violations if v.severity == "error"]
            if verbose:
                print(f" 发现 {len(errors)} 个必须修复项 / {len(violations) - len(errors)} 个建议项")

            if not has_errors(violations):
                break

            messages.append({"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)})
            messages.append({
                "role": "user",
                "content": build_repair_prompt(
                    json.dumps(raw, ensure_ascii=False),
                    "\n".join(v.message for v in errors),
                    self.brand_guide,
                ),
            })

        if draft is None:
            raise RuntimeError("未获得有效结果")

        if has_errors(violations) and AUTO_FIX_FALLBACK:
            draft, autofix_notes = autocorrect(draft, self.site)
            violations = validate(draft, self.site, keywords, brand=self.brand)

        return {
            "sku": sku,
            "site": self.site,
            "model": LLM_MODEL,
            "rounds": rounds,
            "keywords": keywords,
            "keyword_source": kw_source,
            "listing": draft.model_dump(),
            "violations": [
                {"field": v.field, "rule": v.rule, "message": v.message, "severity": v.severity}
                for v in violations
            ],
            "passed": not has_errors(violations),
            "autofix_notes": autofix_notes,
        }


# ---------------------------------------------------------------- 输出

def print_result(result: dict) -> None:
    listing = result["listing"]
    print("\n" + "=" * 72)
    print(f"SKU {result['sku']} ｜ 站点 {result['site']} ｜ 模型 {result['model']}"
          f" ｜ 用了 {result['rounds']} 轮")
    print("=" * 72)

    print(f"\n【标题】{len(listing['title'])} 字符")
    print(f"  {listing['title']}")

    print("\n【五点描述】")
    for i, b in enumerate(listing["bullets"], 1):
        print(f"  {i}. ({len(b)} 字符) {b}")

    print(f"\n【长描述】{len(listing['description'])} 字符")
    print("  " + listing["description"].replace("\n", "\n  "))

    print(f"\n【后台搜索词】{len(listing['search_terms'].encode('utf-8'))} 字节")
    print(f"  {listing['search_terms']}")

    print("\n【合规校验】")
    if not result["violations"]:
        print("  ✓ 全部规则通过")
    else:
        for v in result["violations"]:
            mark = "✗" if v["severity"] == "error" else "!"
            print(f"  {mark} [{v['field']}] ({v['rule']}) {v['message']}")
    print(f"  结论：{'✓ 可直接交付' if result['passed'] else '✗ 仍有必须修复项，需人工介入'}")
    if result["autofix_notes"]:
        print("  兜底裁剪记录：")
        for n in result["autofix_notes"]:
            print(f"    · {n}")


def save_result(result: dict, split: bool = False) -> Path:
    """写入结果仓库（一个文件），需要单独一条时再 --split。"""
    path = results.upsert(result)
    if split:
        for p in results.write_split_files(result):
            print(f"  单独导出：{p.name}")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="AI Listing 生成（亚马逊多站点）")
    ap.add_argument("--sku", help="产品 SKU")
    ap.add_argument("--site", default="US", choices=list(SITE_RULES), help="目标站点")
    ap.add_argument("--list", action="store_true", help="列出所有 SKU")
    ap.add_argument("--split", action="store_true",
                    help="额外导出单独一条的 md + json（默认不导出，避免文件爆炸）")
    args = ap.parse_args()

    if args.list:
        for row in load_products():
            print(f"  {row.get('sku'):<10} {row.get('product_type', ''):<20} {row.get('model_name', '')}")
        return

    if not args.sku:
        ap.error("请指定 --sku，或用 --list 查看可用的 SKU")

    row = find_product(args.sku)
    generator = ListingGenerator(args.site)
    result = generator.generate(row)
    print_result(result)
    path = save_result(result, split=args.split)
    print(f"\n汇总已更新：{path}")
    print(f"可读版本：{results.MD_PATH}")
    print(f"运营表格：{results.csv_path(args.site)}（跑批量时会生成）")
    c = generator.client
    if CACHE_LLM_RESPONSES:
        print(f"大模型调用：缓存命中 {c.hits} 次 / 真实调用 {c.misses} 次")
        if c.hits and not c.misses:
            print("  （本次全部命中缓存，断网也能跑 —— 现场演示靠它）")


if __name__ == "__main__":
    main()
