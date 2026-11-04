#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载：读 config.yaml，自动把蔬东坡凭证注入环境变量"""
from __future__ import annotations
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"


def _load() -> dict:
    if not CONFIG_PATH.exists():
        # 首次运行，从 example 复制
        if EXAMPLE_PATH.exists():
            import shutil
            shutil.copy(EXAMPLE_PATH, CONFIG_PATH)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_cfg = _load()

# 自动把蔬东坡凭证注入环境变量（兼容 sdp_client.py 读 env 的方式）
_sdp = _cfg.get("sdp") or {}
if _sdp.get("appid"):
    os.environ.setdefault("SDP_APPID", _sdp["appid"])
if _sdp.get("secret"):
    os.environ.setdefault("SDP_SECRET", _sdp["secret"])
if _sdp.get("project_code"):
    os.environ.setdefault("SDP_PROJECT_CODE", _sdp["project_code"])


def get(*keys, default=None):
    """嵌套取值: get('storage','library_root')"""
    cur = _cfg
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


def library_root() -> Path:
    return Path(get("storage", "library_root", default="~/qc_library")).expanduser()


def inbox_dir() -> Path:
    return Path(get("storage", "inbox_dir", default="~/Downloads/供应商报告")).expanduser()


def pack_out_dir() -> Path:
    return Path(get("storage", "pack_out_dir", default=str(library_root() / "_packed"))).expanduser()


def ensure_dirs():
    for p in (library_root(), inbox_dir(), pack_out_dir()):
        p.mkdir(parents=True, exist_ok=True)
