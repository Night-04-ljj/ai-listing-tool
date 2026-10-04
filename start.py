#!/usr/bin/env python
"""
AI Listing 工具 · 一键启动器

用法（Windows 直接双击 启动.bat 即可，会自动进虚拟环境重跑自己）：
    py start.py            进入菜单
    py start.py --check    只做环境自检，不安装任何东西
    py start.py --selftest 直接跑离线自测
    py start.py --gen FT48-B --site US
    py start.py --batch US DE
    py start.py --eval
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV_DIR = BASE / ".venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQ = BASE / "requirements.txt"
ENV_FILE = BASE / ".env"
ENV_EXAMPLE = BASE / ".env.example"
OUTPUT_DIR = BASE / "output"
MIRRORS_HINT = "https://pypi.tuna.tsinghua.edu.cn/simple"

if hasattr(sys.stdout, "reconfigure"):  # 避免个别中文/特殊字符导致编码报错
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------- 输出

def h1(t: str) -> None:
    print()
    print("=" * 60)
    print(f"  {t}")
    print("=" * 60)


def step(n: str, t: str) -> None:
    print(f"[{n}] {t}")


def ok(t: str) -> None:
    print(f"    ✓ {t}")


def warn(t: str) -> None:
    print(f"    ! {t}")


def bad(t: str) -> None:
    print(f"    ✗ {t}")


# ----------------------------------------------------------------- 环境

def in_venv() -> bool:
    if not VENV_PY.exists():
        return False
    try:
        return Path(sys.executable).resolve() == VENV_PY.resolve()
    except OSError:
        return False


def deps_ok() -> bool:
    r = subprocess.run(
        [sys.executable, "-c", "import pydantic, dotenv"],
        capture_output=True, cwd=str(BASE),
    )
    return r.returncode == 0


def required_packages() -> list[str]:
    """读出 requirements.txt 里真正需要安装的包（忽略注释、空行、-r/-i 之类的开关）。

    这个项目目前**一个都不需要** —— 所以整个「建虚拟环境 + pip 安装」的流程会被跳过，
    也就不可能卡在下载上了。
    """
    if not REQ.exists():
        return []
    out: list[str] = []
    for line in REQ.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            out.append(line)
    return out


def key_ready() -> bool:
    if not ENV_FILE.exists():
        return False
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("LLM_API_KEY="):
            return bool(s.split("=", 1)[1].strip())
    return False


def cmd_check() -> int:
    h1("环境自检（不会安装或修改任何东西）")
    packages = required_packages()
    print(f"  Python 解释器 : {sys.executable}")
    print(f"  Python 版本   : {sys.version.split()[0]}")
    print(f"  当前目录      : {BASE}")
    print()
    if packages:
        print(f"  需要安装的包  : {', '.join(packages)}")
        print(f"  虚拟环境 .venv: {'已存在' if VENV_PY.exists() else '未创建'}")
        print(f"  当前在 .venv  : {'是' if in_venv() else '否'}")
        if VENV_PY.exists() and in_venv():
            print(f"  依赖是否装齐  : {'是' if deps_ok() else '否'}")
    else:
        print("  需要安装的包  : 无 —— 零依赖，只用 Python 标准库")
        print("  虚拟环境      : 不需要")
        print("  能否直接运行  : 是，不需要联网、不需要 pip")
    print(f"  .env 是否存在 : {'是' if ENV_FILE.exists() else '否'}")
    print(f"  API Key 已填  : {'是' if key_ready() else '否 ← 需要填'}")
    return 0


def ensure_venv() -> None:
    if VENV_PY.exists():
        return
    step("准备", "创建虚拟环境 .venv（只用一次，之后不会重复）")
    try:
        venv.create(VENV_DIR, with_pip=True)
    except Exception as e:  # noqa: BLE001
        bad(f"创建虚拟环境失败：{e}")
        print("\n  可以手动执行：")
        print(f"      py -m venv \"{VENV_DIR}\"")
        sys.exit(1)
    ok(f"已创建：{VENV_DIR}")


def reexec_in_venv(argv: list[str]) -> None:
    step("准备", "切换到虚拟环境，重新启动本程序")
    try:
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *argv])
    except Exception as e:  # noqa: BLE001
        bad(f"切换虚拟环境失败：{e}")
        sys.exit(1)


def ensure_deps() -> None:
    if deps_ok():
        ok("依赖已安装，跳过")
        return
    step("依赖", "安装第三方包")
    print("      依次尝试清华源、阿里源、官方源，任意一个成功即可……")
    # --timeout / --retries：某个包下不动时快速失败，而不是卡住不动
    common = ["-m", "pip", "install", "-r", str(REQ),
              "--timeout", "30", "--retries", "2", "--disable-pip-version-check"]
    mirrors = [
        (["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], "清华源"),
        (["-i", "https://mirrors.aliyun.com/pypi/simple/"], "阿里源"),
        ([], "官方源"),
    ]
    for i, (extra, label) in enumerate(mirrors, 1):
        print(f"\n      第 {i}/{len(mirrors)} 次尝试（{label}）：")
        r = subprocess.run([sys.executable, *common, *extra], cwd=str(BASE))
        if r.returncode == 0:
            ok("依赖安装完成")
            return
        warn(f"{label} 没成功，换下一个")
    bad("三个源都没装成功。请检查网络（或换手机热点），也可以手动执行：")
    print(f"      \"{VENV_PY}\" -m pip install -r requirements.txt -i {MIRRORS_HINT}")
    sys.exit(1)


def ensure_env() -> None:
    step("配置", "检查配置文件 .env")
    if ENV_FILE.exists():
        ok(".env 已存在")
        if not key_ready():
            warn("但 LLM_API_KEY 还是空的 —— 生成文案前必须填上")
            print("      获取方式（任选一个，都有免费额度）：")
            print("        · 硅基流动   https://siliconflow.cn")
            print("        · DeepSeek   https://platform.deepseek.com")
            print("        · 阿里云百炼 https://bailian.console.aliyun.com")
            if os.name == "nt":
                try:
                    if input("      现在用记事本打开 .env 去填？(y/n) ").strip().lower() == "y":
                        os.startfile(ENV_FILE)  # type: ignore[attr-defined]
                except (EOFError, OSError):
                    pass
        return

    if not ENV_EXAMPLE.exists():
        bad("找不到 .env.example，无法生成 .env")
        sys.exit(1)
    ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    ok("已从 .env.example 生成 .env")
    warn("现在需要填 API Key：打开 .env，把 LLM_API_KEY= 后面补上你的 key")
    if os.name == "nt":
        try:
            if input("      现在用记事本打开 .env？(y/n) ").strip().lower() == "y":
                os.startfile(ENV_FILE)  # type: ignore[attr-defined]
        except (EOFError, OSError):
            pass


# ----------------------------------------------------------------- 执行

def run_script(script: str, args: list[str] | None = None) -> int:
    cmd = [sys.executable, str(BASE / script), *(args or [])]
    try:
        return subprocess.run(cmd, cwd=str(BASE)).returncode
    except KeyboardInterrupt:
        print("\n（已中断）")
        return 130


def pause() -> None:
    try:
        input("\n按回车返回菜单…")
    except (EOFError, KeyboardInterrupt):
        pass


MENU = """
你要做什么？

  [1] 离线自测          —— 不用 API key、不花钱，验证规则引擎（建议第一次先跑这个）
  [2] 看有哪些 SKU      —— 列出产品表里的型号
  [3] 生成一个 Listing  —— US 站（需要已填 API key）
  [4] 生成一个 Listing  —— DE 站
  [5] 批量生成          —— US + DE 全部 SKU
  [6] 跑评测            —— 生成合规率 / 关键词覆盖率报告
  [7] 打开输出文件夹    —— 看生成的结果
  [8] 打开 .env         —— 填/改 API key
  [9] 打开网页界面      —— 浏览器里生成 / 多语言并排对比 / 导出（推荐）
  [10] 结果仓库统计     —— 有多少条、合规多少、有没有旧文件
  [11] 和真实在售对比   —— 拿真实 Amazon 标题当基准，量 AI 标题差在哪
  [q] 退出
"""


def menu() -> int:
    while True:
        h1("AI Listing 工具")
        print(f"  Python : {sys.version.split()[0]}（零依赖，无需虚拟环境）")
        print(f"  API Key: {'已配置 ✓' if key_ready() else '未配置 ✗（第 3-6、9 项会失败）'}")
        print(MENU)
        try:
            choice = input("请输入序号：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in {"q", "quit", "exit", "0"}:
            print("再见 👋")
            return 0
        elif choice == "1":
            run_script("selftest.py")
        elif choice == "2":
            run_script("generate.py", ["--list"])
        elif choice == "3":
            sku = _ask_sku()
            if sku:
                run_script("generate.py", ["--sku", sku, "--site", "US"])
        elif choice == "4":
            sku = _ask_sku()
            if sku:
                run_script("generate.py", ["--sku", sku, "--site", "DE"])
        elif choice == "5":
            run_script("batch.py", ["--site", "US", "--site", "DE"])
        elif choice == "6":
            run_script("evaluate.py")
        elif choice == "7":
            _open_dir(OUTPUT_DIR)
        elif choice == "8":
            _open_file(ENV_FILE)
        elif choice == "9":
            print("  启动网页服务，浏览器会自动打开。")
            print("  要停止服务，回到这个窗口按 Ctrl+C。")
            print()
            run_script("web.py")
        elif choice == "10":
            run_script("results.py", ["--stats"])
        elif choice == "11":
            run_script("compare.py")
        else:
            print("  没看懂，请输入 1-11 或 q")

        pause()


def _ask_sku() -> str:
    try:
        sku = input("  输入 SKU（不知道就先选 [2] 看列表，直接回车取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return sku


def _open_dir(path: Path) -> None:
    if not path.exists():
        warn("输出目录还不存在，先跑一次生成（第 3 或 5 项）")
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    ok(f"已打开：{path}")


def _open_file(path: Path) -> None:
    if not path.exists():
        warn(f"{path.name} 不存在，先跑一次安装流程")
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    ok(f"已打开：{path.name}")


# ----------------------------------------------------------------- 入口

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="AI Listing 工具一键启动器")
    ap.add_argument("--check", action="store_true", help="只做环境自检")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--gen", metavar="SKU")
    ap.add_argument("--site", default="US")
    ap.add_argument("--batch", nargs="*", metavar="SITE")
    ap.add_argument("--eval", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        return cmd_check()

    packages = required_packages()

    if not packages:
        # ---- 零依赖模式：不需要虚拟环境，也不需要 pip，更不会卡在下载上 ----
        h1("零依赖模式")
        print("  本项目只用 Python 标准库，不需要安装任何第三方包。")
        print("  → 跳过虚拟环境、跳过 pip、不需要联网。")
    else:
        if not in_venv():
            h1("首次启动，正在准备运行环境（只做这一次）")
            ensure_venv()
            reexec_in_venv(argv)
            return 1  # execv 成功的话不会走到这里
        ensure_deps()

    ensure_env()

    # --- 无参数：进菜单 ---
    if not any([args.selftest, args.gen, args.batch is not None, args.eval]):
        return menu()
    if args.selftest:
        return run_script("selftest.py")
    if args.gen:
        return run_script("generate.py", ["--sku", args.gen, "--site", args.site])
    if args.batch is not None:
        sites = args.batch or ["US"]
        cmd_args: list[str] = []
        for s in sites:
            cmd_args += ["--site", s.upper()]
        return run_script("batch.py", cmd_args)
    if args.eval:
        return run_script("evaluate.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n（已中断）")
        sys.exit(130)
