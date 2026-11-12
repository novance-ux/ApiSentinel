from __future__ import annotations

import json

from typer.testing import CliRunner

from apitest import __version__
from apitest.cli import app
from tests.conftest import SPECS

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_inspect_prints_table() -> None:
    result = runner.invoke(app, ["inspect", str(SPECS / "buggy_api.yaml")])
    assert result.exit_code == 0, result.output
    assert "Buggy Inventory API" in result.output
    assert "/items/{item_id}" in result.output
    assert "11 endpoints" in result.output


def test_inspect_json() -> None:
    result = runner.invoke(app, ["inspect", str(SPECS / "petstore.yaml"), "--json"])
    assert result.exit_code == 0, result.output
    endpoints = json.loads(result.output)
    assert {e["operation_id"] for e in endpoints} == {
        "listPets",
        "createPet",
        "getPet",
        "deletePet",
    }


def test_inspect_missing_file_exits_2() -> None:
    result = runner.invoke(app, ["inspect", "nope.yaml"])
    assert result.exit_code == 2
