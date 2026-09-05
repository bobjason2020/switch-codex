"""Switch-codex 全局常量与默认值。"""
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
LOGS = ROOT / "logs"

# 品牌 / 路由身份
PROJECT_NAME = "Switch-codex"
PROVIDER_NAME = "switch-codex"
DEFAULT_MASTER_KEY = "sk-switch-codex"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4100


def env_host() -> str:
    return os.environ.get("SW_HOST") or DEFAULT_HOST


def env_port() -> int:
    raw = os.environ.get("SW_PORT") or os.environ.get("SR_PORT")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return DEFAULT_PORT


def provider_base_url() -> str:
    return os.environ.get("SW_BASE_URL") or f"http://{env_host()}:{env_port()}/v1"

# 日志 / 历史保留
ERROR_LOG_RETENTION_HOURS = 24
REQUEST_LOG_RETENTION_DAYS = 30
AVAIL_HISTORY_RETENTION_DAYS = 14
CACHE_PRIORITY_TTL_SEC = 3600
# 掉缓存判定：同会话同上游相邻请求间隔超过该秒数视为缓存自然过期，不判掉缓存。
CACHE_MISS_MAX_GAP_SEC = 3600
# 请求上下文在客户端重算时可能少量回退；在该容差内仍视为同一缓存前缀。
# 明显缩短则归类为上下文收缩，不把它误报成掉缓存。
CACHE_MISS_INPUT_SHRINK_TOLERANCE_TOKENS = 2048
# 掉缓存少于该 token 数时视为正常波动，不计入掉缓存统计。
CACHE_MISS_MIN_LOST_TOKENS = 10_000
# 持久化判定版本。规则变化时历史日志必须重新计算，而不是沿用旧 false。
CACHE_MISS_RULE_VERSION = 4
ERROR_LOG_BODY_MAX_BYTES = 1024 * 1024
ERROR_LOG_ATTEMPT_BODY_MAX = 2000
LOG_STREAM_BUF_MAX = 4 * 1024 * 1024

# 模型 / 探测
DEFAULT_MODEL = "openai"
LOCAL_DIRECT = "local-direct"
# DeepSeek 通用路由池 + 池下两个并列客户端模型
DEEPSEEK_POOL = "deepseek"
DEEPSEEK_CLIENT_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEEPSEEK_DEFAULT_MODEL_MAP = [
    {"model": m, "actual": m} for m in DEEPSEEK_CLIENT_MODELS
]
# Grok 通用路由池 + 池下客户端模型（同 DeepSeek 的池/模型分层结构）
GROK_POOL = "grok"
GROK_CLIENT_MODELS = ("grok-4.6",)
GROK_DEFAULT_MODEL_MAP = [
    {"model": m, "actual": m} for m in GROK_CLIENT_MODELS
]
# grok-4.6 挂牌价（USD/1M）：NewAPI model_ratio=1 / completion=3 / cache=0.25
GROK_DEFAULT_PRICING = {
    "grok-4.6": {
        "input_per_m": 2.0,
        "output_per_m": 6.0,
        "cache_read_per_m": 0.5,
    }
}
DEFAULT_PROBE_INTERVAL_SEC = 300
PROBE_MULTIPLIER_THRESHOLD = 0.1
DEFAULT_CLIENT_MODELS = (
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-astra",
)
DEFAULT_CLIENT_MODEL = DEFAULT_CLIENT_MODELS[-1]
DEFAULT_OPENAI_ALL_MODEL_MAP = [
    {"model": "gpt-5.6-luna", "actual": ""},
    {"model": "gpt-5.6-terra", "actual": ""},
    {"model": "gpt-5.6-sol", "actual": ""},
    {"model": "gpt-6-astra", "actual": ""},
]

# 内置默认单价（USD / 1M tokens）：仓库 clone 下来即可直接算账，无需手工填。
# 数据库 config.pricing 里只保存与这里不同的覆盖项；同名模型存在覆盖时整条替换。
DEFAULT_PRICING: dict[str, dict[str, Any]] = {
    "gpt-5.6-luna": {
        "input_per_m": 0.2,
        "output_per_m": 1.2,
        "cache_read_per_m": 0.025,
        "cache_creation_per_m": 0.25,
        "long_context": {
            "threshold": 272000,
            "input_per_m": 0.4,
            "output_per_m": 1.8,
            "cache_read_per_m": 0.05,
            "cache_creation_per_m": 0.5,
        },
    },
    "gpt-5.6-terra": {
        "input_per_m": 2.0,
        "output_per_m": 12.0,
        "cache_read_per_m": 0.25,
        "cache_creation_per_m": 2.5,
        "long_context": {
            "threshold": 272000,
            "input_per_m": 4.0,
            "output_per_m": 18.0,
            "cache_read_per_m": 0.5,
            "cache_creation_per_m": 5.0,
        },
    },
    "gpt-5.6-sol": {
        "input_per_m": 5.0,
        "output_per_m": 30.0,
        "cache_read_per_m": 0.5,
        "cache_creation_per_m": 6.25,
        "long_context": {
            "threshold": 272000,
            "input_per_m": 10.0,
            "output_per_m": 45.0,
            "cache_read_per_m": 1.0,
            "cache_creation_per_m": 12.5,
        },
    },
    "gpt-6-astra": {
        "input_per_m": 10.0,
        "output_per_m": 60.0,
        "cache_read_per_m": 1.0,
        "cache_creation_per_m": 12.5,
    },
    "deepseek-v4-flash": {
        "input_per_m": 1.5,
        "output_per_m": 4.5,
        "cache_read_per_m": 0.05,
    },
    "deepseek-v4-pro": {
        "input_per_m": 3.0,
        "output_per_m": 9.0,
        "cache_read_per_m": 0.1,
    },
    **{model: dict(vals) for model, vals in GROK_DEFAULT_PRICING.items()},
}

# NewAPI 最低扣费：1 quota = $1 / 500000
MIN_REAL_COST = 0.000002

# NewAPI 倍率探测默认
DEFAULT_NEWAPI_PROBE = {
    "enabled": True,
    "interval_sec": 600,
    "base_url": "",
    "group": "",
    "upstream_name": "",
    "access_token": "",
    "priority_bias": 0.0,
}

# UI 配色（历史可用性色块）
COLOR_MID = "#c2410c"
COLOR_BAD = "#ef4444"

# 管理认证
DEFAULT_ADMIN_PASSWORD = "admin123"
SESSION_TTL_DAYS = 7
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_MINUTES = 5

DEFAULT_CONFIG = {
    "master_key": DEFAULT_MASTER_KEY,
    "host": env_host(),
    "port": env_port(),
    "timeout_sec": 120,
    "active_model": DEFAULT_CLIENT_MODEL,
    "probe": {
        "interval_sec": DEFAULT_PROBE_INTERVAL_SEC,
        "models": {},
    },
}
