import pytest

from waldur_core.permissions.models import RoleManager


@pytest.fixture(autouse=True)
def _clear_role_cache():
    RoleManager.clear_cache()
    yield
    RoleManager.clear_cache()
