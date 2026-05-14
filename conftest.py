import pytest

from waldur_core.permissions.models import RoleManager


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow / load tests (deselect with '-m \"not slow\"')",
    )
    config.addinivalue_line(
        "markers",
        "lab: requires access to the OpenStack lab (skipped without .secrets/lab-tenant-creds.env)",
    )


@pytest.fixture(autouse=True)
def _clear_role_cache():
    RoleManager.clear_cache()
    yield
    RoleManager.clear_cache()


@pytest.fixture(autouse=True)
def _immediate_on_commit(monkeypatch):
    # transaction.on_commit callbacks are not executed in TestCase because
    # each test is wrapped in a transaction that gets rolled back instead of
    # committed. Patch on_commit to execute callbacks immediately so that
    # tests behave the same way as production code.
    from django.db import transaction

    monkeypatch.setattr(transaction, "on_commit", lambda func, using=None: func())
