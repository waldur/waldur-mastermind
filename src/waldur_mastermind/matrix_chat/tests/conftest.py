"""Auto-skip Matrix integration tests unless explicitly opted into.

`test_integration.py` carries `pytestmark = pytest.mark.matrix_integration`.
That marker is registered in the workspace conftest.py — but registering a
marker doesn't skip anything by itself. This hook does: every test tagged
with `matrix_integration` is dynamically marked `skip` during collection
unless the run was invoked with `-m matrix_integration` (or any other `-m`
expression that selects the marker).

The marker was introduced to replace a module-import-time httpx probe that
could wedge a CI shard indefinitely on runner networks that drop packets to
unbound ports — collection-time `skip` keeps the matrix_chat test surface
fast and reliable in CI while the integration tests stay easy to run
locally against `docker/matrix-dev/`.
"""

import pytest

_OPT_IN_KEYWORDS = ("matrix_integration",)


def pytest_collection_modifyitems(config, items):
    selected = config.getoption("-m") or ""
    if any(kw in selected for kw in _OPT_IN_KEYWORDS):
        # Operator opted into the marker explicitly; let pytest's normal
        # -m filtering handle selection.
        return

    skip_marker = pytest.mark.skip(
        reason="matrix_integration tests skipped by default; "
        "run with `-m matrix_integration` against the docker/matrix-dev stack."
    )
    for item in items:
        if "matrix_integration" in item.keywords:
            item.add_marker(skip_marker)
