from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tagging-owned tables go to a scratch file so pytest never writes into db/tagging.db.
# The Cortico snapshot stays the real, read-only one.
os.environ.setdefault("TAGGING_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_tagging.db"))

import pytest  # noqa: E402

from app import models  # noqa: E402, F401  registers tables on Base
from app.db import Base, tagging_engine  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _create_tagging_tables():
    Base.metadata.create_all(bind=tagging_engine)
