"""Auto-skip vcsim integration tests unless explicitly opted into.

The `test_vcsim_*.py` modules carry `pytestmark = pytest.mark.vcsim`. That
marker is registered in the workspace conftest.py — but registering a marker
doesn't skip anything by itself. This hook does: every test tagged `vcsim` is
dynamically marked `skip` during collection unless the run was invoked with
`-m vcsim` (or any other `-m` expression that names the marker).

The sharded unit suite runs with `-m 'not slow'` (see `tests/waldur-test`),
which does not name `vcsim`, so those shards skip these tests without needing a
simulator. The dedicated CI job opts in with `-m vcsim` and brings up vcsim as a
service; locally, `docker/vcsim-dev/` does the same.

This mirrors `matrix_chat/tests/conftest.py`, and for the same reason: deciding
at collection time avoids any network I/O while pytest is still collecting,
which is what wedges a CI shard on runners that drop packets to unbound ports.
"""

import pytest

_OPT_IN_KEYWORDS = ("vcsim",)


def pytest_collection_modifyitems(config, items):
    selected = config.getoption("-m") or ""
    if any(keyword in selected for keyword in _OPT_IN_KEYWORDS):
        # Operator opted into the marker explicitly; let pytest's normal
        # -m filtering handle selection.
        return

    skip_marker = pytest.mark.skip(
        reason="vcsim tests skipped by default; "
        "run with `-m vcsim` against the docker/vcsim-dev stack."
    )
    for item in items:
        if "vcsim" in item.keywords:
            item.add_marker(skip_marker)
