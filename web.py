"""
简易 Web 页面：生成、查看、多语言对比、统计、导出。

启动：
    py web.py                # 然后浏览器打开 http://127.0.0.1:8765
    py web.py --port 9000
    py web.py --no-browser   # 不自动打开浏览器

【为什么用标准库 http.server，而不是 Flask / Streamlit】
这个项目从一开始就坚持「零第三方依赖」：不用 pip、不联网也能跑起来。
一个本地单人用的工具，标准库的 ThreadingHTTPServer 完全够用，
换来的是**在任何一台装了 Python 的机器上双击就能演示**。

【前后端分工】
服务端只做一件事：**生成一条 Listing**（一个 (SKU, 站点) 组合）。
批量、进度、多语言并排，全部由前端循环调用完成。
这样服务端保持简单（无状态、无长连接），前端能给出真实的进度反馈，
也不用为了一个进度条去引入 SSE 或 WebSocket。
"""
from __future__ import annotations

import argparse
import json
import threading
import traceback
import urllib.parse
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import results
from config import DATA_DIR, LLM_API_KEY, LLM_MODEL
from generate import INTERNAL_FIELDS, ListingGenerator, load_products, parse_product_text
from rules import SITE_RULES, keyword_source, keywords_for, load_keywords

DEFAULT_PORT = 8765
PRODUCTS_CSV = DATA_DIR / "products.csv"

# 站点展示顺序（英语放前面，欧洲次之，日本最后）
SITE_ORDER = ["US", "UK", "DE", "JP"]


# ---------------------------------------------------------------- 业务封装

@lru_cache(maxsize=8)
def _generator(site: str) -> ListingGenerator:
    return ListingGenerator(site)


def _products() -> list[dict]:
    """给前端用的产品列表。

    【为什么要兜底】导入的产品表可能是任意表头（中文的、或字段完全不同的），
    如果只认 product_type 这一个列名，网页上的产品卡片就会**一片空白**，
    看起来像导入失败。所以副标题按优先级找，实在没有就用第一个有值的列。
    """
    out = []
    for row in load_products():
        subtitle = ""
        for key in ("product_type", "产品类型", "产品名称", "name", "title", "model_name"):
            v = str(row.get(key) or "").strip()
            if v:
                subtitle = v
                break
        if not subtitle:
            for k, v in row.items():
                if k not in INTERNAL_FIELDS and str(v or "").strip():
                    subtitle = f"{k}: {v}"
                    break
        out.append({
            "sku": str(row.get("sku") or "").strip(),
            "product_type": subtitle[:48],
            "model_name": str(row.get("model_name") or "").strip(),
            "category": str(row.get("category") or "").strip(),
            "tv_size_range": str(row.get("tv_size_range") or "").strip(),
            "max_load_kg": str(row.get("max_load_kg") or "").strip(),
        })
    return out


def _sites() -> list[dict]:
    kw = load_keywords()
    out = []
    for code in SITE_ORDER:
        if code not in SITE_RULES:
            continue
        ruled = SITE_RULES[code]
        cats = [c for c in kw if not c.startswith("_") and kw[c].get(code)]
        out.append({
            "code": code,
            "language": ruled["language"],
            "title_max": ruled["title_max"],
            "keywords_ready": len(cats),
            "keywords_total": len([c for c in kw if not c.startswith("_")]),
        })
    return out


def _generate(sku: str, site: str) -> dict:
    if site not in SITE_RULES:
        raise ValueError(f"不支持的站点：{site}")
    product = next((p for p in load_products() if p.get("sku") == sku), None)
    if product is None:
        raise ValueError(f"产品表里没有 SKU：{sku}")
    _require_key()
    result = _generator(site).generate(product, verbose=False)
    results.upsert(result)
    return result


def _require_key() -> None:
    if not LLM_API_KEY:
        raise RuntimeError(
            "还没有配置 API Key。请在 ai-listing-tool/.env 里填上 LLM_API_KEY，"
            "然后重启本页面。"
        )


# ---------------------------------------------------------------- 临时产品

def _generate_temp(text: str, site: str, category: str = "",
                   sku: str = "") -> dict:
    """临时产品：粘贴一段参数 → 直接生成，**不写进结果仓库**。

    【使用场景】对方临时给你一个新型号，你没有时间去做产品表，
    直接把参数粘进来就能出文案。这也是这个工具"通用性"的证明：
    同一套 生成→校验→修复→兜底 的闭环，不依赖预置的产品数据。
    """
    if site not in SITE_RULES:
        raise ValueError(f"不支持的站点：{site}")
    text = (text or "").strip()
    if len(text) < 4:
        raise ValueError("请至少粘贴一点产品参数")
    _require_key()

    facts, extra = parse_product_text(text)
    if not facts and not extra:
        raise ValueError("没能从这段文字里解析出任何内容")

    keywords = keywords_for(category, site) if category else []
    kw_source = keyword_source(category, site) if category else "临时产品未指定类目 —— 由模型按目标语言自行生成"

    result = _generator(site).generate_from_facts(
        facts=facts,
        sku=sku.strip() or "临时产品",
        keywords=keywords,
        kw_source=kw_source,
        extra_text=extra,
        verbose=False,
    )
    result["is_temp"] = True
    result["parsed_facts"] = facts
    result["extra_text_chars"] = len(extra)
    return result


# ---------------------------------------------------------------- 产品表导入 / 导出

def products_backup_path():
    return PRODUCTS_CSV.with_name("products.csv.bak")


def product_snapshots() -> list:
    """历史快照（products.csv.<时间戳>.bak），按时间倒序。"""
    if not PRODUCTS_CSV.parent.exists():
        return []
    return sorted(PRODUCTS_CSV.parent.glob("products.csv.*.bak"), reverse=True)


def _snapshot_products(keep: int = 3) -> None:
    """覆盖产品表前先留痕。

    【为什么要留两级，而不是只留一个 .bak】
    只留一个 .bak 的话，**连续导入两次就会把原始表弄丢**：
    第一次导入时 .bak = 原始表；第二次导入时 .bak 被覆盖成第一次导入的内容，
    原始表就永久没了 —— 我自己测试时就踩到了这个坑。
    所以现在：
        products.csv.bak           上一版（「恢复上一个产品表」用这个，一键回滚）
        products.csv.<时间戳>.bak   历史快照，最多留 3 份
    """
    import shutil

    if not PRODUCTS_CSV.exists():
        return
    shutil.copy2(PRODUCTS_CSV, products_backup_path())

    from datetime import datetime

    # 注意：快照名要保证唯一。只精确到秒的话，同一秒内的两次导入会互相覆盖，
    # 第二次就把「原始表」那份快照冲掉了 —— 这个坑我实测踩到过。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = PRODUCTS_CSV.with_name(f"products.csv.{stamp}.bak")
    n = 2
    while target.exists():
        target = PRODUCTS_CSV.with_name(f"products.csv.{stamp}_{n}.bak")
        n += 1
    shutil.copy2(PRODUCTS_CSV, target)
    for old in product_snapshots()[keep:]:
        old.unlink()


def _import_products(text: str) -> dict:
    """导入一张产品表 CSV。

    【为什么这里要花点功夫】
    运营从 Excel 导出的 CSV 有几个常见坑，都得处理：
      1. 编码可能是 GBK（中文 Excel 默认）—— 前端已经做了自动识别
      2. **表头带空格或 BOM** —— 列名变成 " sku" 之后，代码里 `row["sku"]` 就取不到值
      3. **列名大小写不统一** —— 真实表里 sku / SKU / Sku 都有人写。
         原来的写法是精确匹配 "sku"，遇到 `SKU` 会直接拒绝导入，
         而且报错信息只说"必须包含 sku 列"，用的人会一脸问号。
      4. 有空 sku 的行 —— 跳过，但要**告诉用户跳了几行**，不能悄悄吞掉
    """
    import csv as _csv
    import io

    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        raise ValueError("文件是空的")
    reader = _csv.DictReader(io.StringIO(text))
    raw_fields = reader.fieldnames or []
    fields = [f.strip().lstrip("\ufeff") for f in raw_fields]
    if not fields:
        raise ValueError("CSV 没有表头")

    # 大小写不敏感地找 sku 列，并统一改名为 "sku"（后面的代码只认这个名字）
    sku_field = next((f for f in fields if f.lower() == "sku"), None)
    if sku_field is None:
        raise ValueError(
            f"CSV 里找不到 sku 列（大小写不敏感）。当前表头：{raw_fields}"
        )
    out_fields = ["sku"] + [f for f in fields if f != sku_field]

    rows: list[dict] = []
    skipped = 0
    for raw in reader:
        row = {(k or "").strip().lstrip("\ufeff"): (v or "").strip()
               for k, v in raw.items()}
        sku = row.get(sku_field, "").strip()
        if not sku:
            # 整行都是空的就静默跳过（Excel 常见的尾部空行），有内容但缺 sku 的要计数
            if any((v or "").strip() for v in row.values()):
                skipped += 1
            continue
        rec = {"sku": sku}
        for f in fields:
            if f != sku_field:
                rec[f] = row.get(f, "")
        rows.append(rec)
    if not rows:
        raise ValueError("CSV 里没有有效数据行（每行必须有 sku）")

    # 覆盖前先留痕（上一版 + 历史快照）
    _snapshot_products()

    with PRODUCTS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  已导入产品表：{len(rows)} 个 SKU，{len(out_fields)} 列"
          + (f"，跳过 {skipped} 行（缺 sku）" if skipped else "")
          + "（旧表已备份，可一键恢复）", flush=True)
    return {
        "count": len(rows),
        "skipped": skipped,
        "columns": out_fields,
        "skus": [r["sku"] for r in rows],
        "has_backup": products_backup_path().exists(),
        "snapshots": len(product_snapshots()),
    }


def _restore_products() -> dict:
    import shutil

    bak = products_backup_path()
    if not bak.exists():
        raise ValueError("没有可恢复的备份（还没有导入过产品表）")
    shutil.copy2(bak, PRODUCTS_CSV)
    rows = load_products()
    print(f"  已从备份恢复产品表：{len(rows)} 个 SKU", flush=True)
    return {"count": len(rows), "skus": [r["sku"] for r in rows]}


def _products_info() -> dict:
    rows = load_products()
    cols = list(rows[0].keys()) if rows else []
    return {
        "count": len(rows),
        "columns": cols,
        "skus": [r.get("sku", "") for r in rows],
        "categories": sorted({r.get("category", "") for r in rows if r.get("category")}),
        "has_backup": products_backup_path().exists(),
        "snapshots": len(product_snapshots()),
    }


def _stats() -> dict:
    data = results.load_all()
    by_site: dict[str, dict] = {}
    for r in data.values():
        s = by_site.setdefault(r["site"], {"n": 0, "ok": 0, "rounds": 0.0, "cov": 0.0})
        s["n"] += 1
        s["ok"] += 1 if r.get("passed") else 0
        s["rounds"] += r.get("rounds", 0)
        kws = r.get("keywords") or []
        listing = r["listing"]
        from rules import coverage_of

        s["cov"] += coverage_of(
            kws, [listing["title"], *listing["bullets"], listing["description"]]
        )
    for s in by_site.values():
        n = s["n"] or 1
        s["rounds"] = round(s["rounds"] / n, 2)
        s["cov"] = round(s["cov"] / n, 3)
    return {
        "total": len(data),
        "passed": sum(1 for r in data.values() if r.get("passed")),
        "by_site": by_site,
        "model": LLM_MODEL,
        "api_key_ready": bool(LLM_API_KEY),
        "cache_calls": len(list((results.OUTPUT_DIR / "llm_cache").glob("*.json")))
        if (results.OUTPUT_DIR / "llm_cache").exists() else 0,
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ListingTool/1.0"

    # ---- 工具 ----
    def _send(self, body: bytes, ctype: str, code: int = 200,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _download(self, data: bytes, filename: str, ctype: str) -> None:
        quoted = urllib.parse.quote(filename)
        self._send(data, ctype, extra={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        })

    def log_message(self, fmt, *args):  # 只保留关键请求，别刷屏
        if "/api/" in (self.path or ""):
            print(f"  [{self.command}] {self.path}")

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path)
        route = path.path
        qs = urllib.parse.parse_qs(path.query)
        try:
            if route in ("/", "/index.html"):
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/state":
                self._json({
                    "products": _products(),
                    "products_info": _products_info(),
                    "sites": _sites(),
                    "stats": _stats(),
                })
            elif route == "/api/results":
                self._json(results.load_all())
            elif route == "/api/export":
                kind = (qs.get("kind") or ["csv"])[0]
                if kind == "md":
                    data = results.rebuild_markdown().read_bytes()
                    self._download(data, "all_listings.md", "text/markdown; charset=utf-8")
                elif kind == "products":
                    # 导出当前产品表，顺便当「模板」用
                    self._download(PRODUCTS_CSV.read_bytes(), "products.csv",
                                   "text/csv; charset=utf-8")
                else:
                    site = (qs.get("site") or ["US"])[0]
                    p = results.write_csv(results.load_all(), site)
                    if not p:
                        self._json({"error": f"{site} 站点还没有生成结果"}, 404)
                        return
                    self._download(p.read_bytes(), p.name, "text/csv; charset=utf-8")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        try:
            if route == "/api/generate":
                sku = payload.get("sku", "")
                site = payload.get("site", "")
                print(f"  生成中：{sku} · {site} …", flush=True)
                result = _generate(sku, site)
                print(f"    完成：{'合规' if result['passed'] else '有必须修复项'}"
                      f"（{result['rounds']} 轮）", flush=True)
                self._json({"ok": True, "result": result, "stats": _stats()})
            elif route == "/api/generate-temp":
                site = payload.get("site", "")
                sku = payload.get("sku", "")
                print(f"  临时产品生成中 · {site} …", flush=True)
                result = _generate_temp(
                    payload.get("text", ""), site,
                    category=(payload.get("category") or "").strip(),
                    sku=sku,
                )
                print(f"    完成：解析出 {len(result['parsed_facts'])} 个参数，"
                      f"{'合规' if result['passed'] else '有必须修复项'}"
                      f"（{result['rounds']} 轮）", flush=True)
                self._json({"ok": True, "result": result})
            elif route == "/api/import-products":
                info = _import_products(payload.get("csv", ""))
                self._json({"ok": True, "info": info,
                            "products": _products(),
                            "products_info": _products_info()})
            elif route == "/api/restore-products":
                info = _restore_products()
                self._json({"ok": True, "info": info,
                            "products": _products(),
                            "products_info": _products_info()})
            elif route == "/api/keyword-source":
                self._json({"source": keyword_source(payload.get("category", ""),
                                                     payload.get("site", "US"))})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


# ---------------------------------------------------------------- 前端页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Listing 生成器</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --line:#e4e7ec; --ink:#1f2430; --muted:#6b7280;
    --brand:#2f6fed; --brand-d:#1f4fb8; --ok:#0f9d58; --warn:#d98200; --err:#d93025;
    --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 22px;
         display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:5}
  header h1{font-size:17px;margin:0;font-weight:600}
  header .spacer{flex:1}
  .badge{font-size:12px;color:var(--muted);background:#eef1f5;border-radius:20px;padding:3px 10px}
  .badge.ok{color:var(--ok);background:#e8f6ee}
  .badge.bad{color:var(--err);background:#fdecea}
  button{font:inherit;border:1px solid var(--line);background:#fff;color:var(--ink);
         border-radius:8px;padding:7px 14px;cursor:pointer;transition:.15s}
  button:hover{border-color:#c8ced8}
  button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
  button.primary:hover{background:var(--brand-d)}
  button:disabled{opacity:.5;cursor:not-allowed}
  main{padding:20px 22px 60px;max-width:1500px;margin:0 auto}
  .stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
        padding:10px 14px;min-width:110px}
  .stat b{display:block;font-size:19px;font-weight:600}
  .stat span{color:var(--muted);font-size:12px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
         padding:16px;margin-bottom:16px}
  .panel h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;
            text-transform:uppercase;letter-spacing:.04em}
  .skus{display:flex;gap:8px;flex-wrap:wrap}
  .chip{border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer;
        background:#fbfcfd;user-select:none}
  .chip small{color:var(--muted);display:block;font-size:11px}
  .chip.on{border-color:var(--brand);background:#eef3fe;box-shadow:0 0 0 1px var(--brand) inset}
  .sites{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
  .sites .chip.on{border-color:var(--ok);background:#eefaf2;box-shadow:0 0 0 1px var(--ok) inset}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .hint{color:var(--muted);font-size:12px}
  #progress{display:none;background:#fff;border:1px solid var(--line);
            border-radius:var(--radius);padding:12px 16px;margin-bottom:16px}
  #bar{height:6px;background:#eef1f5;border-radius:4px;overflow:hidden;margin-top:8px}
  #bar>i{display:block;height:100%;width:0;background:var(--brand);transition:width .3s}
  .cols{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
        padding:16px;overflow:hidden}
  .card h3{margin:0 0 4px;font-size:15px}
  .card .meta{color:var(--muted);font-size:12px;margin-bottom:12px}
  .field{margin-bottom:12px}
  .field .lab{font-size:12px;color:var(--muted);display:flex;justify-content:space-between}
  .field .val{background:#fafbfc;border:1px solid var(--line);border-radius:8px;
              padding:8px 10px;margin-top:4px;white-space:pre-wrap;word-break:break-word}
  ol.bullets{margin:4px 0 0;padding-left:20px}
  ol.bullets li{margin-bottom:5px}
  .tag{display:inline-block;font-size:11px;border-radius:6px;padding:1px 7px;margin-left:6px}
  .tag.error{background:#fdecea;color:var(--err)}
  .tag.warn{background:#fef4e5;color:var(--warn)}
  .tag.ok{background:#e8f6ee;color:var(--ok)}
  details{margin-top:8px}
  summary{cursor:pointer;color:var(--muted);font-size:12px}
  ul.v{margin:8px 0 0;padding-left:18px;font-size:12px;color:#444}
  .empty{color:var(--muted);text-align:center;padding:40px 0}
  textarea,input:not([type]){font:inherit;border:1px solid var(--line);border-radius:8px;
    padding:8px 10px;background:#fff;color:var(--ink);width:100%}
  textarea{min-height:140px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
  label.btn-like{display:inline-block;border:1px solid var(--line);background:#fff;
    border-radius:8px;padding:7px 14px;cursor:pointer;white-space:nowrap}
  label.btn-like:hover{border-color:#c8ced8}
  .lbl{font-size:12px;color:var(--muted);display:block;margin:0 0 3px}
  .flexrow{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
  .flexrow>.grow{flex:1;min-width:320px}
  .flexrow>.side{width:230px}
  .section-title{font-size:13px;color:var(--muted);margin:22px 0 8px;font-weight:600}
  .filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
  .filters .chip{padding:4px 10px;font-size:12px}
  .filters .chip small{display:inline;margin-left:4px}
  .card.hl{border-color:var(--brand);box-shadow:0 0 0 1px var(--brand) inset}
  .card .pin{font-size:11px;color:var(--brand)}
</style>
</head>
<body>
<header>
  <h1>🔧 AI Listing 生成器</h1>
  <span class="badge" id="b-model">模型 —</span>
  <span class="badge" id="b-key">API Key —</span>
  <span class="badge" id="b-cache">缓存 0</span>
  <span class="spacer"></span>
  <button onclick="exportMd()">导出汇总 .md</button>
  <button onclick="refresh()">刷新数据</button>
</header>

<main>
  <div class="stats" id="stats"></div>

  <div class="panel">
    <h2>① 选产品</h2>
    <div class="skus" id="skus"></div>
  </div>

  <div class="panel">
    <h2>② 选语言 / 站点（可多选，用于并排对比）</h2>
    <div class="sites" id="sites"></div>
    <div class="row" style="margin-top:14px">
      <button class="primary" id="btn-gen" onclick="generate()">生成选中的产品 × 选中的语言</button>
      <button id="btn-batch" onclick="batchAll()">批量生成：全部产品 × 选中的语言</button>
      <span class="hint" id="hint"></span>
    </div>
  </div>

  <div class="panel">
    <h2>③ 临时产品 —— 粘贴参数直接生成（不写进产品表，适合试新品）</h2>
    <div class="flexrow">
      <div class="grow">
        <label class="lbl">产品参数（两种写法都支持，可以混着写）</label>
        <textarea id="temp-text" placeholder="型号: XG-200&#10;承重: 75kg&#10;VESA: 200x200 至 800x500 mm&#10;材质: 冷轧钢&#10;&#10;也可以直接粘一整段说明书原文，例如：&#10;适用于 40 到 90 英寸电视，安装前请确认墙体为混凝土或实心砖墙。"></textarea>
      </div>
      <div class="side">
        <label class="lbl">类目（可选，决定用哪套关键词）</label>
        <input id="temp-cat" list="cat-list" placeholder="如 tv_wall_mount">
        <datalist id="cat-list"></datalist>
        <label class="lbl" style="margin-top:10px">SKU / 名字（可选）</label>
        <input id="temp-sku" placeholder="如 XG-200">
        <button class="primary" id="btn-temp" onclick="generateTemp()"
                style="margin-top:12px;width:100%">生成临时产品</button>
        <div class="hint" style="margin-top:8px">用上面勾选的语言逐个生成，结果只显示在下方，不写进结果仓库。</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>④ 产品表</h2>
    <div class="row">
      <span class="hint" id="prod-info">—</span>
      <span style="flex:1"></span>
      <button onclick="location.href='/api/export?kind=products'">下载当前产品表（可当模板）</button>
      <label class="btn-like">导入产品表 CSV
        <input type="file" id="csv-file" accept=".csv" onchange="importCsv(this)" hidden>
      </label>
      <button id="btn-restore" onclick="restoreProducts()">恢复上一个产品表</button>
    </div>
    <div class="hint" style="margin-top:8px">
      导入后**立即生效，不用重启**；旧表会自动备份，导错了一键恢复。
      支持 Excel 导出的 CSV（自动识别 UTF-8 / GBK 编码）。
    </div>
  </div>

  <div id="progress">
    <div id="ptext" class="hint">准备中…</div>
    <div id="bar"><i></i></div>
  </div>

  <div id="temp-results" class="cols"></div>

  <div class="section-title">
    <span id="result-title">已保存的文案</span>
    <span id="result-count" style="font-weight:400"></span>
  </div>
  <div class="filters" id="result-filters"></div>
  <div id="results" class="cols"></div>
</main>

<script>
let STATE = {products:[], sites:[], stats:{}};
let RESULTS = {};
let selSku = null;          // 要生成哪个产品
let filterSku = null;       // 结果区只看哪个产品；null = 全部
let selSites = new Set(["US"]);

const $ = id => document.getElementById(id);
const esc = s => (s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(url, opts){
  const r = await fetch(url, opts);
  const j = await r.json().catch(()=>({error:"返回内容不是 JSON"}));
  if(!r.ok && j.error) throw new Error(j.error);
  return j;
}

function renderState(){
  const s = STATE.stats || {};
  const by = s.by_site || {};
  const cov = k => by[k] ? (by[k].cov*100).toFixed(0)+"%" : "—";
  $("stats").innerHTML = [
    ["文案总数", s.total ?? 0],
    ["合规", (s.passed ?? 0) + "/" + (s.total ?? 0)],
    ["US 关键词覆盖", cov("US")],
    ["DE 关键词覆盖", cov("DE")],
    ["缓存调用", s.cache_calls ?? 0],
  ].map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");

  $("b-model").textContent = "模型 " + (s.model || "—");
  const key = $("b-key");
  key.textContent = s.api_key_ready ? "API Key 已配置" : "API Key 未配置";
  key.className = "badge " + (s.api_key_ready ? "ok" : "bad");
  $("b-cache").textContent = "缓存 " + (s.cache_calls ?? 0) + " 次";

  $("skus").innerHTML = STATE.products.map(p =>
    `<div class="chip ${p.sku===selSku?'on':''}" onclick="pickSku('${p.sku}')">
       ${esc(p.sku)}<small>${esc(p.product_type)}</small></div>`).join("");

  const pi = STATE.products_info || {};
  $("prod-info").textContent =
    `当前：${pi.count ?? 0} 个 SKU ｜ ${(pi.columns||[]).length} 列` +
    ` ｜ 可回滚：${pi.has_backup ? "是" : "否"}${pi.snapshots ? `（历史快照 ${pi.snapshots} 份）` : ""}`;
  $("cat-list").innerHTML = (pi.categories || [])
    .map(c => `<option value="${esc(c)}">`).join("");
  $("btn-restore").disabled = !pi.has_backup;

  $("sites").innerHTML = STATE.sites.map(s =>
    `<div class="chip ${selSites.has(s.code)?'on':''}" onclick="toggleSite('${s.code}')">
       ${esc(s.language)}<small>${s.code} · 标题上限 ${s.title_max} · 词库 ${s.keywords_ready}/${s.keywords_total} 类目</small>
     </div>`).join("");

  $("hint").textContent = !s.api_key_ready
    ? "配置好 .env 里的 LLM_API_KEY 之后才能生成（查看结果和导出不受影响）"
    : "";
}

function pickSku(sku){
  // selSku = 要生成哪个产品；点了之后结果区要重新"置顶高亮"，
  // 但**不动 filterSku** —— 筛不筛选是用户自己的选择，不能被选产品带跑。
  selSku = sku;
  renderState();
  renderResults();
}
function toggleSite(code){
  selSites.has(code) ? selSites.delete(code) : selSites.add(code);
  if(selSites.size === 0) selSites.add(code);
  renderState();
}

function card(result, hl){
  const l = result.listing, v = result.violations || [];
  const errs = v.filter(x=>x.severity==="error").length;
  const warns = v.length - errs;
  const bytes = new TextEncoder().encode(l.search_terms).length;
  return `<div class="card ${hl ? "hl" : ""}">
    <h3>${esc(result.sku)} · ${esc(result.site)}</h3>
    <div class="meta">
      ${hl ? '<span class="tag ok">当前选中</span>' : ''}
      ${result.is_temp ? '<span class="tag warn">临时产品 · 未保存</span>' : ''}
      ${result.passed ? '<span class="tag ok">合规 ✓</span>' : '<span class="tag error">有必须修复项</span>'}
      ${errs?'<span class="tag error">'+errs+' error</span>':''}
      ${warns?'<span class="tag warn">'+warns+' warning</span>':''}
      <span class="tag">修复 ${result.rounds} 轮</span>
      <br>关键词来源：${esc(result.keyword_source || "—")}
    </div>
    <div class="field">
      <div class="lab"><span>Title</span><span>${l.title.length} 字符</span></div>
      <div class="val">${esc(l.title)}</div>
    </div>
    <div class="field">
      <div class="lab"><span>Bullet Points</span><span>${l.bullets.length} 条</span></div>
      <ol class="bullets">${l.bullets.map(b=>`<li>${esc(b)}</li>`).join("")}</ol>
    </div>
    <div class="field">
      <div class="lab"><span>Description</span><span>${l.description.length} 字符</span></div>
      <div class="val">${esc(l.description)}</div>
    </div>
    <div class="field">
      <div class="lab"><span>Search Terms</span><span>${bytes} 字节 / 250</span></div>
      <div class="val">${esc(l.search_terms)}</div>
    </div>
    ${v.length?`<details><summary>校验提示（${v.length}）</summary><ul class="v">${
      v.map(x=>`<li>${x.severity==="error"?"✗":"!"} [${esc(x.field)}] ${esc(x.message)}</li>`).join("")
    }</ul></details>`:""}
    ${(result.autofix_notes||[]).length?`<details><summary>兜底裁剪记录</summary><ul class="v">${
      result.autofix_notes.map(n=>`<li>${esc(n)}</li>`).join("")}</ul></details>`:""}
  </div>`;
}

function renderResults(){
  const keys = Object.keys(RESULTS);
  const box = $("results");
  $("result-title").textContent = "已保存的文案";

  if(!keys.length){
    box.innerHTML = `<div class="empty">还没有生成任何文案。选好产品和语言，点上面的生成按钮。</div>`;
    $("result-count").textContent = "";
    renderFilters();
    return;
  }

  /* ⚠️ 这里原来有个 bug：直接按 selSku 过滤，导致"选中某个产品后，
     其他产品的文案全都看不见了"，而且界面上没有任何提示，像是数据丢了。
     现在改成：默认显示全部；**当前选中的产品排在最前面并高亮**；
     想只看某个产品，用下面对筛选条。 */
  let show = keys.map(k => RESULTS[k]);
  if(filterSku) show = show.filter(r => r.sku === filterSku);
  show.sort((a, b) => {
    const pa = a.sku === selSku ? 0 : 1, pb = b.sku === selSku ? 0 : 1;
    if(pa !== pb) return pa - pb;                       // 选中的排最前
    if(a.sku !== b.sku) return a.sku.localeCompare(b.sku);
    return a.site.localeCompare(b.site);
  });

  const nSel = selSku ? Object.values(RESULTS).filter(r => r.sku === selSku).length : 0;
  $("result-count").textContent =
    `　共 ${keys.length} 条，当前显示 ${show.length} 条`
    + (selSku ? `（${selSku} 的 ${nSel} 条已置顶高亮）` : "");

  box.innerHTML = show.map(r => card(r, r.sku === selSku)).join("");
  renderFilters();
}

/* 产品筛选条：默认"全部"，可以只看某一个产品 */
function renderFilters(){
  const box = $("result-filters");
  const all = Object.values(RESULTS);
  const skus = [...new Set(all.map(r => r.sku))].sort();
  if(skus.length <= 1){ box.innerHTML = ""; return; }
  const chip = (label, n, on, arg) =>
    `<div class="chip ${on ? "on" : ""}" onclick="setFilter(${arg})">${esc(label)}<small>${n}</small></div>`;
  box.innerHTML =
    chip("全部", all.length, filterSku === null, "null")
    + skus.map(s => chip(s, all.filter(r => r.sku === s).length, filterSku === s, `'${s}'`)).join("");
}

function setFilter(sku){ filterSku = sku; renderResults(); }

async function loadResults(){ RESULTS = await api("/api/results"); renderResults(); }
async function refresh(){ STATE = await api("/api/state"); renderState(); await loadResults(); }

function progress(on, text, pct){
  $("progress").style.display = on ? "block" : "none";
  if(text!=null) $("ptext").textContent = text;
  if(pct!=null) $("bar").firstElementChild.style.width = pct+"%";
}
function lock(on){
  $("btn-gen").disabled = on; $("btn-batch").disabled = on; $("btn-temp").disabled = on;
}

async function genOne(sku, site){
  const j = await api("/api/generate", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sku, site})
  });
  if(!j.ok) throw new Error(j.error || "生成失败");
  RESULTS[sku+"|"+site] = j.result;
  return j.result;
}

async function runQueue(jobs, label){
  lock(true);
  let i = 0, ok = 0, fail = [];
  for(const job of jobs){
    i++;
    progress(true, `${label} ${i}/${jobs.length}：${job.sku} · ${job.site} …`, (i-1)/jobs.length*100);
    try{ await genOne(job.sku, job.site); ok++; }
    catch(e){ fail.push(`${job.sku}·${job.site}: ${e.message}`); }
  }
  progress(true, `完成 ${ok}/${jobs.length}` + (fail.length?`　失败 ${fail.length} 条：${fail[0]}`:""), 100);
  renderResults();
  STATE = await api("/api/state"); renderState();
  lock(false);
  setTimeout(()=>progress(false), 4000);
}

async function generate(){
  if(!selSku) return alert("请先选一个产品");
  const jobs = [...selSites].map(s=>({sku:selSku, site:s}));
  await runQueue(jobs, "正在生成");
}

async function batchAll(){
  if(!confirm(`将生成 ${STATE.products.length} 个产品 × ${selSites.size} 种语言 = ${STATE.products.length*selSites.size} 条。\n同样的内容第二次跑会命中缓存，不额外花钱。继续？`)) return;
  const jobs = [];
  for(const p of STATE.products) for(const s of selSites) jobs.push({sku:p.sku, site:s});
  await runQueue(jobs, "批量生成");
}

/* ---------------- 临时产品 ---------------- */

async function generateTemp(){
  const text = $("temp-text").value.trim();
  if(text.length < 4) return alert("请先在左边的框里粘贴产品参数");
  const sites = [...selSites];
  const box = $("temp-results");
  // 给临时结果自己的标题（原来是去改"已保存的文案"那个标题，会让两个区域混淆）
  box.innerHTML = `<div class="section-title" style="grid-column:1/-1;margin-top:0">`
    + `临时产品结果（未保存，不会写进记录）</div>`;
  lock(true);
  let i = 0, fail = [];
  for(const site of sites){
    i++;
    progress(true, `临时产品 ${i}/${sites.length}：${site} …`, (i-1)/sites.length*100);
    try{
      const j = await api("/api/generate-temp", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({text, site,
          category: $("temp-cat").value.trim(),
          sku: $("temp-sku").value.trim()})
      });
      if(!j.ok) throw new Error(j.error || "生成失败");
      box.insertAdjacentHTML("beforeend", card(j.result));
    }catch(e){
      fail.push(site);
      box.insertAdjacentHTML("beforeend",
        `<div class="card"><h3>${site}</h3>
           <div class="meta" style="color:var(--err)">${esc(e.message)}</div></div>`);
    }
  }
  progress(true, `临时产品完成 ${i-fail.length}/${i}` + (fail.length?`　失败：${fail.join(", ")}`:""), 100);
  lock(false);
  setTimeout(()=>progress(false), 4000);
}

/* ---------------- 产品表导入 / 恢复 ---------------- */

function readFileSmart(file){
  /* Excel 导出的中文 CSV 经常是 GBK，直接按 UTF-8 读会乱码。
     这里先试 UTF-8（严格模式），失败再试 GBK。 */
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => {
      for(const enc of ["utf-8", "gbk"]){
        try{ return resolve(new TextDecoder(enc, {fatal:true}).decode(fr.result)); }
        catch(e){ /* 换下一个编码 */ }
      }
      resolve(new TextDecoder("utf-8").decode(fr.result));
    };
    fr.onerror = reject;
    fr.readAsArrayBuffer(file);
  });
}

async function importCsv(input){
  const file = input.files && input.files[0];
  if(!file) return;
  try{
    const text = await readFileSmart(file);
    if(!confirm(`导入「${file.name}」？\n\n当前产品表会被替换。\n旧表会自动备份，导错了可以点「恢复上一个产品表」。`)){
      input.value = ""; return;
    }
    const j = await api("/api/import-products", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({csv: text})
    });
    if(!j.ok) throw new Error(j.error || "导入失败");
    alert(`导入成功：${j.info.count} 个 SKU`
      + (j.info.skipped ? `（跳过 ${j.info.skipped} 行：缺 sku）` : "")
      + `\n列名：${j.info.columns.join(", ")}`
      + `\n旧表已备份，导错了可以点「恢复上一个产品表」。`);
    STATE.products = j.products;
    STATE.products_info = j.products_info;
    selSku = (j.products[0] || {}).sku || null;
    renderState(); renderResults();
  }catch(e){
    alert("导入失败：" + e.message);
  }
  input.value = "";
}

async function restoreProducts(){
  if(!confirm("用备份恢复产品表？当前的会被覆盖。")) return;
  try{
    const j = await api("/api/restore-products", {
      method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"
    });
    if(!j.ok) throw new Error(j.error || "恢复失败");
    alert(`已恢复：${j.info.count} 个 SKU`);
    STATE.products = j.products;
    STATE.products_info = j.products_info;
    selSku = (j.products[0] || {}).sku || null;
    renderState(); renderResults();
  }catch(e){
    alert("恢复失败：" + e.message);
  }
}

function exportMd(){ location.href = "/api/export?kind=md"; }
</script>

<script>
(async function init(){ await refresh(); })();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 启动

def main() -> None:
    ap = argparse.ArgumentParser(description="AI Listing 生成器 · 简易 Web 页面")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    results.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"

    print("=" * 58)
    print("  AI Listing 生成器 · Web 页面")
    print("=" * 58)
    print(f"  地址        ：{url}")
    print(f"  模型        ：{LLM_MODEL}")
    print(f"  API Key     ：{'已配置' if LLM_API_KEY else '未配置（生成会失败，但可以查看已有结果）'}")
    print(f"  结果仓库    ：{results.JSON_PATH}")
    print(f"  按 Ctrl+C   ：停止服务")
    print("=" * 58)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
