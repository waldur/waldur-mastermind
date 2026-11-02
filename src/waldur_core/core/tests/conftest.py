"""Skip the access coverage report unless it is asked for.

`test_access_coverage.py` lists every API route that declares nothing about
who may call it, and fails while any remain. It runs in its own CI job, allowed
to fail until every app has declared its routes, so the sharded unit suite must
not run it: tests marked `access_coverage` are skipped unless the run names the
marker (`-m access_coverage`).
"""

import pytest

_OPT_IN_KEYWORDS = ("access_coverage",)


def pytest_collection_modifyitems(config, items):
    selected = config.getoption("-m") or ""
    if any(keyword in selected for keyword in _OPT_IN_KEYWORDS):
        return

    skip_marker = pytest.mark.skip(
        reason="access coverage report; run with `-m access_coverage`."
    )
    for item in items:
        if "access_coverage" in item.keywords:
            item.add_marker(skip_marker)
