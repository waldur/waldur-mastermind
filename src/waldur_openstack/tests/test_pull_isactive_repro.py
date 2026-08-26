"""Reproduction: deactivating a ServiceSettings does not stop plugin-level pulls.

Evidence for a bug report. Every assertion below documents CURRENT behaviour,
so the whole file passes today; the ones marked BUG are the ones that should
flip once the gap is fixed.
"""

import pytest

from waldur_core.core.enums import CoreStates
from waldur_core.structure import tasks as structure_tasks
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack import models as openstack_models
from waldur_openstack import tasks as openstack_tasks
from waldur_openstack.tests import factories as openstack_factories


@pytest.fixture
def dead_backend(db):
    """A deactivated ServiceSettings with one tenant that failed to sync."""
    settings = openstack_factories.SettingsFactory(is_active=False)
    project = structure_factories.ProjectFactory()
    tenant = openstack_factories.TenantFactory(
        service_settings=settings,
        project=project,
        state=CoreStates.ERRED,
        backend_id="gone-from-backend",
    )
    return {"settings": settings, "tenant": tenant, "project": project}


def test_service_level_pull_honours_is_active(dead_backend):
    """Control: the ServiceSettings-level task DOES respect is_active."""
    pulled = structure_tasks.ServiceListPullTask().get_pulled_objects()
    assert dead_backend["settings"].id not in set(pulled.values_list("id", flat=True))


@pytest.mark.parametrize(
    "task_class",
    [
        openstack_tasks.TenantResourcesListPullTask,
        openstack_tasks.TenantSubresourcesListPullTask,
        openstack_tasks.TenantPropertiesListPullTask,
    ],
)
def test_BUG_tenant_pulls_ignore_is_active(dead_backend, task_class):
    """BUG: every per-tenant pull still schedules work against a dead backend.

    These three tasks run hourly / 2-hourly / daily. Each attempt blocks for
    ~131s when the endpoint is unreachable, and ERRED is inside the pulled
    state set, so it retries forever.
    """
    pulled = task_class().get_pulled_objects()
    assert dead_backend["tenant"].id in set(pulled.values_list("id", flat=True))


def test_BUG_erred_state_guarantees_infinite_retry(dead_backend):
    """BUG: failure puts a tenant in ERRED, and ERRED is itself pulled."""
    pulled_states = (
        openstack_tasks.TenantResourcesListPullTask().get_pulled_objects().query
    )
    assert dead_backend["tenant"].state == CoreStates.ERRED
    assert dead_backend["tenant"].id in set(
        openstack_tasks.TenantResourcesListPullTask()
        .get_pulled_objects()
        .values_list("id", flat=True)
    ), pulled_states


def test_BUG_quota_pull_has_no_filtering_at_all(dead_backend):
    """BUG: TenantPullQuotas selects on state=OK only - no is_active, no backend_id."""
    dead_backend["tenant"].state = CoreStates.OK
    dead_backend["tenant"].save(update_fields=["state"])
    selected = openstack_models.Tenant.objects.filter(state=CoreStates.OK)
    assert dead_backend["tenant"].id in set(selected.values_list("id", flat=True))
    # and it is selected even with an empty backend_id, where no call can succeed
    dead_backend["tenant"].backend_id = ""
    dead_backend["tenant"].save(update_fields=["backend_id"])
    selected = openstack_models.Tenant.objects.filter(state=CoreStates.OK)
    assert dead_backend["tenant"].id in set(selected.values_list("id", flat=True))


def test_BUG_zombie_invariant_is_undetected(dead_backend):
    """BUG: marketplace TERMINATED + live plugin row is pulled, and nothing checks it.

    This is the state a failed staff force_destroy leaves behind
    (marketplace/utils.py:160-183) and the shape of all 88 production rows.
    """
    marketplace_factories.ResourceFactory(
        project=dead_backend["project"],
        scope=dead_backend["tenant"],
        state=Resource.States.TERMINATED,
    )
    pulled = openstack_tasks.TenantResourcesListPullTask().get_pulled_objects()
    assert dead_backend["tenant"].id in set(pulled.values_list("id", flat=True))


def test_no_timeout_is_configured_on_the_session(db):
    """BUG: keystoneauth Session.timeout is None, so a dead host blocks ~131s."""
    from waldur_openstack import session as openstack_session

    built = openstack_session.TimedSession(verify=False)
    assert built.timeout is None
