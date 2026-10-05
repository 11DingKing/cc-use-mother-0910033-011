#!/usr/bin/env python3
"""启动多机构执业冲突后端服务。

用法：python3 tools/run_server.py
环境变量：PC_DB_PATH（SQLite 路径，默认 ./practice_conflict.db）、PORT（默认 8000）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from practice_conflict.api import create_app  # noqa: E402

app = create_app(os.environ.get("PC_DB_PATH", str(ROOT / "practice_conflict.db")))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
