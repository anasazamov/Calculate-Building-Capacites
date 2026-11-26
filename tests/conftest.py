import os
from pathlib import Path

import pytest

from tests import fixtures

# The replayed responses in tests/data/raw were recorded with the former reader, served as "qwen3.8-27b"
# on port 8001; the served name is part of every request's cache key. Until they are re-recorded with
# Gemma (python -m tests.record_replay on the server), the tests present the reader under that name.
os.environ.setdefault("FEECALC_MODEL1_NAME", "qwen3.8-27b")


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("samples")
    fixtures.build_conclusion(d / "demo_conclusion.pdf")
    fixtures.build_project(d / "demo_project.pdf")
    return d
