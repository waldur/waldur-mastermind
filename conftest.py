import pytest

from waldur_core.permissions.enums import TEAM_VIEW_PERMISSIONS
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
    config.addinivalue_line(
        "markers",
        "vcsim: requires a running vCenter simulator (vcsim). "
        "Opt-in via -m vcsim; the sharded unit suite skips by default.",
    )


@pytest.fixture(autouse=True)
def _clear_role_cache():
    RoleManager.clear_cache()
    yield
    RoleManager.clear_cache()


@pytest.fixture(autouse=True)
def _system_roles_view_team(monkeypatch):
    # permissions.yaml grants CUSTOMER.VIEW_TEAM / PROJECT.VIEW_TEAM to every
    # customer- and project-scoped system role, and import_roles applies it on
    # every deployment. Tests never run import_roles (CI even skips the
    # migrations), so system roles are created bare by get_system_role and
    # every team-listing test would otherwise have to grant it by hand. Mirror
    # the deployment here; a test that needs the permission absent deletes it.
    original = RoleManager.get_system_role

    def get_system_role(self, name, content_type):
        cache_key = name.value if hasattr(name, "value") else name
        cached = cache_key in RoleManager._cache
        role = original(self, name, content_type)
        if not cached:
            permission = TEAM_VIEW_PERMISSIONS.get(
                (role.content_type.app_label, role.content_type.model)
            )
            if permission is not None:
                role.add_permission(permission)
        return role

    monkeypatch.setattr(RoleManager, "get_system_role", get_system_role)


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
