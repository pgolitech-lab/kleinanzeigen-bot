"""Pytest config + общие фикстуры.

sys.path bootstrap чтобы тесты могли импортить modules/* и web/app.py
без устанавливаемого пакета.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
