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
    config.addinivalue_line(
        "markers",
        "matrix_integration: requires a running Matrix homeserver (Tuwunel). "
        "Opt-in via -m matrix_integration; CI skips by default.",
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


@pytest.fixture(autouse=True)
def _eager_component_usage_billing(monkeypatch):
    # In production, the post_save signal for ComponentUsage schedules
    # process_component_usage_billing on the celery worker. In tests there's
    # no broker, so patch this one task's .delay to call the function inline.
    # This keeps tests on the same code path as production without globally
    # enabling CELERY_TASK_ALWAYS_EAGER (which would wake up many unrelated
    # tasks across the codebase).
    from waldur_mastermind.marketplace import billing_usage

    monkeypatch.setattr(
        billing_usage.process_component_usage_billing,
        "delay",
        lambda *args, **kwargs: billing_usage.process_component_usage_billing(
            *args, **kwargs
        ),
    )
