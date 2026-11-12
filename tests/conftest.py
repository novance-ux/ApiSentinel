from __future__ import annotations

from pathlib import Path

import pytest

from apitest.models import ApiSpec
from apitest.spec.loader import load_spec

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "examples" / "specs"


@pytest.fixture(scope="session")
def buggy_spec() -> ApiSpec:
    return load_spec(str(SPECS / "buggy_api.yaml"))


@pytest.fixture(scope="session")
def petstore_spec() -> ApiSpec:
    return load_spec(str(SPECS / "petstore.yaml"))


@pytest.fixture(scope="session")
def library_spec() -> ApiSpec:
    return load_spec(str(SPECS / "library.json"))
