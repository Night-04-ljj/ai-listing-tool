"""
集中配置：模型、站点、重试策略。

【面试要点】所有平台规则都不写死在代码里，而是集中成可配置的规则表。
因为亚马逊各站点的字符上限、类目要求会变，把它做成配置才能跟得上变化。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """极简 .env 读取器 —— 自己写，不依赖 python-dotenv。

    【为什么要自己写】
    整个项目要做到「零依赖」：不用 pip、不用虚拟环境、不联网就能跑。
    那么连读 .env 也不能靠第三方包。这段只有十几行，规则也很简单：
      - 忽略空行和以 # 开头的注释
      - KEY=VALUE，两侧空格去掉
      - 值被引号包起来就去掉引号
      - 已经存在的环境变量优先（不覆盖），方便临时用命令行覆盖配置
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

# ---------------- 大模型（任何 OpenAI 兼容接口） ----------------
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.4"))  # 文案要一点创造性，但不能太飘

# ---------------- 校验与重试 ----------------
# 生成 → 校验 → 把「具体违规项」喂回去重写，最多几轮
MAX_REPAIR_ROUNDS = int(_env("MAX_REPAIR_ROUNDS", "2"))
# 最后一次修复失败时，是否用确定性的规则做兜底裁剪（截断超长标题等）
AUTO_FIX_FALLBACK = _env("AUTO_FIX_FALLBACK", "true").lower() == "true"

# ---------------- 大模型响应缓存 ----------------
# 打开后，同一个 Prompt 只会真正调用一次模型，之后直接读本地缓存。
# 两个作用：
#   1) 省钱省时间（改规则后重跑不用重复付费）
#   2) ⭐ 线下现场演示时断网也能跑完整流程 —— 这条最实用
CACHE_LLM_RESPONSES = _env("CACHE_LLM_RESPONSES", "true").lower() == "true"
CACHE_DIR = OUTPUT_DIR / "llm_cache"
