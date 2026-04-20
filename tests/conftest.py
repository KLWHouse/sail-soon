"""Test-wide helpers: reset cached Settings/engine between modules so each
module can point at its own SQLite file without cross-contamination."""
from __future__ import annotations

import pytest


def _reset_sailsoon_caches() -> None:
    from sailsoon import config, db

    config.get_settings.cache_clear()
    config.load_locations.cache_clear()
    config.load_rules.cache_clear()

    if db._engine is not None:
        db._engine.dispose()
    db._engine = None
    db._SessionLocal = None


@pytest.fixture(autouse=True, scope="module")
def _isolate_caches():
    _reset_sailsoon_caches()
    yield
    _reset_sailsoon_caches()
