"""The patch dialog learns which patches are the base source's from the backend."""

import json
import re

from src import config_store, main, patcher
from tests.conftest import make_config

BASE_REPO = "MorpheApp/morphe-patches"
OTHER_REPO = "other/patches"


def write_source(repo: str, patch_name: str) -> None:
    path = patcher.get_source_paths(repo)[".json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    patch = {"name": patch_name, "compatiblePackages": {"com.app": ["1.0"]}}
    path.write_text(json.dumps({"patches": [patch]}))


def configure_sources(data_dir, sources: list[dict]) -> None:
    config = make_config()
    config["sources"] = sources
    config_store.save_config(config)


def test_base_flag_follows_the_repo_not_the_display_name(data_dir):
    write_source(BASE_REPO, "base-patch")
    write_source(OTHER_REPO, "other-patch")
    configure_sources(
        data_dir,
        [
            {"name": "MorpheApp Patches", "repo": BASE_REPO, "active": True},
            {"name": "Morphe Patches", "repo": OTHER_REPO, "active": True},
        ],
    )

    patches = {p["name"]: p for p in main.get_patches_for_app("com.app")}

    assert patches["base-patch"]["is_base"] is True
    assert patches["other-patch"]["is_base"] is False


def test_base_flag_ignores_repo_case(data_dir):
    write_source("morpheapp/Morphe-Patches", "base-patch")
    configure_sources(
        data_dir,
        [{"name": "X", "repo": "morpheapp/Morphe-Patches", "active": True}],
    )

    patches = main.get_patches_for_app("com.app")

    assert patches[0]["is_base"] is True


def test_display_name_stays_in_the_response(data_dir):
    write_source(BASE_REPO, "base-patch")
    configure_sources(
        data_dir, [{"name": "MorpheApp Patches", "repo": BASE_REPO, "active": True}]
    )

    assert main.get_patches_for_app("com.app")[0]["source"] == "MorpheApp Patches"


def test_template_does_not_recognise_the_base_source_by_name():
    config = make_config()
    template = main.templates.get_template("index.html")

    html = template.render(config=config, available_apps=[], metadata={}, request=None)

    assert "Morphe Patches" not in html
    assert re.search(r"\.is_base", html)
