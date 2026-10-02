"""Determinism of get_supported_versions across processes and source naming."""

import json
import os
import subprocess
import sys
from pathlib import Path

from src import patcher

BASE_REPO = "MorpheApp/morphe-patches"


def write_source(repo: str, patch_name: str, versions: list[str]) -> None:
    patch = {"name": patch_name, "compatiblePackages": {"com.app": versions}}
    path = patcher.get_source_paths(repo)[".json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"patches": [patch]}))


def pick(config: dict) -> list[str]:
    return patcher.get_supported_versions("com.app", config, quiet=True)[0]


def test_base_source_recognised_by_repo_not_display_name(data_dir):
    write_source(BASE_REPO, "base-patch", ["1.0", "2.0"])
    write_source("other/patches", "other-patch", ["3.0"])
    config = {
        "sources": [
            {"name": "MorpheApp Patches", "repo": BASE_REPO, "active": True},
            {"name": "Other", "repo": "other/patches", "active": True},
        ],
        "apps": [{"id": "com.app", "include_patches": ["base-patch", "other-patch"]}],
    }

    assert pick(config) == ["3.0"]


def test_base_source_with_no_selected_patch_still_provides(data_dir):
    write_source(BASE_REPO, "base-patch", ["1.0", "2.0"])
    write_source("other/patches", "other-patch", ["2.0", "3.0"])
    config = {
        "sources": [
            {"name": "Renamed", "repo": BASE_REPO, "active": True},
            {"name": "Other", "repo": "other/patches", "active": True},
        ],
        "apps": [{"id": "com.app", "include_patches": ["other-patch"]}],
    }

    assert pick(config) == ["2.0"]


_SCRIPT = """
import json, sys
from pathlib import Path
from src import patcher
patcher.DATA_DIR = Path(sys.argv[1])
names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
sources = []
for index, name in enumerate(names):
    repo = f"own/{name.lower()}"
    path = patcher.get_source_paths(repo)[".json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    patch = {"name": f"p{index}", "compatiblePackages": {"com.app": [f"{index}.0"]}}
    path.write_text(json.dumps([patch]))
    sources.append({"name": name, "repo": repo, "active": True})
config = {
    "sources": sources,
    "apps": [{"id": "com.app", "include_patches": [f"p{i}" for i in range(5)]}],
}
print(patcher.get_supported_versions("com.app", config, quiet=True)[0][0])
"""


def test_result_is_identical_across_hash_seeds(tmp_path):
    results = set()
    for seed in range(8):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        completed = subprocess.run(
            [sys.executable, "-c", _SCRIPT, str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=Path(__file__).parent.parent,
            check=True,
        )
        results.add(completed.stdout.strip().splitlines()[-1])

    assert results == {"4.0"}
