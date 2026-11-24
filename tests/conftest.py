from pathlib import Path

import pytest

from tests import fixtures


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("samples")
    fixtures.build_conclusion(d / "demo_conclusion.pdf")
    fixtures.build_project(d / "demo_project.pdf")
    return d
