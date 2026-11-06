from __future__ import annotations

from pathlib import Path

import pytest

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DIR
