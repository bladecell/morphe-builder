"""Guard the ShardX pin without launching a browser."""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_shardx_is_pinned_to_an_exact_version() -> None:
    """The engine API is private-ish, so the dependency must be pinned with ==."""
    dependencies = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]

    pins = [item for item in dependencies if item.lower().startswith("shardx")]

    assert len(pins) == 1
    assert pins[0].startswith("shardx==")
