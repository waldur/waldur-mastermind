import pytest

from waldur_core.permissions.models import RoleManager


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow / load tests (deselect with '-m \"not slow\"')",
    )


@pytest.fixture(autouse=True)
def _clear_role_cache():
    RoleManager.clear_cache()
    yield
    RoleManager.clear_cache()
