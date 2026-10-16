"""通用工具：配置加载、路径、日志。"""
from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "intel.sqlite3"

USER_AGENT = (
    "CompetitiveIntelBot/1.0 (+internal research; "
    "contact: set-your-email@example.com)"
)

# 合规：请求间隔，避免对目标站点造成压力
DELAY_RANGE = (2.0, 5.0)


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(name)


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR):
        d.mkdir(parents=True, exist_ok=True)


def polite_sleep(delay_range: tuple[float, float] = DELAY_RANGE) -> None:
    """合规延迟：随机等待，降低对方站点压力。"""
    time.sleep(random.uniform(*delay_range))


def load_yaml(path: Path) -> dict:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
