"""Deactivating a ServiceSettings stops the plugin-level pulls of its tenants,
and a tenant whose marketplace resource is terminated is not pulled at all.

An unreachable backend blocks each pull for minutes, and ERRED is inside the
pulled state set, so without these filters a failed tenant is retried forever.
"""

from unittest import mock

import pytest
from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.structure import tasks as structure_tasks
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack import models as openstack_models
from waldur_openstack import tasks as openstack_tasks
from waldur_openstack.tests import factories as openstack_factories

TENANT_LIST_PULL_TASKS = [
    openstack_tasks.TenantResourcesListPullTask,
    openstack_tasks.TenantSubresourcesListPullTask,
    openstack_tasks.TenantPropertiesListPullTask,
]


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


@pytest.fixture
def live_tenant(db):
    return openstack_factories.TenantFactory(state=CoreStates.ERRED)


def scheduled_tenants(task_class):
    with mock.patch.object(task_class, "pull_task") as pull_task:
        task_class().run()
    return [call.kwargs["args"][0] for call in pull_task().apply_async.call_args_list]


def make_zombie(tenant, project):
    """The state a failed staff force_destroy leaves behind: the marketplace
    resource is TERMINATED while the tenant row stays."""
    marketplace_factories.ResourceFactory(
        project=project,
        scope=tenant,
        state=Resource.States.TERMINATED,
    )


def test_service_level_pull_honours_is_active(dead_backend):
    """Control: the ServiceSettings-level task respects is_active."""
    pulled = structure_tasks.ServiceListPullTask().get_pulled_objects()
    assert dead_backend["settings"].id not in set(pulled.values_list("id", flat=True))


@pytest.mark.parametrize("task_class", TENANT_LIST_PULL_TASKS)
def test_tenant_pulls_skip_inactive_settings(dead_backend, live_tenant, task_class):
    scheduled = scheduled_tenants(task_class)

    assert core_utils.serialize_instance(dead_backend["tenant"]) not in scheduled
    assert core_utils.serialize_instance(live_tenant) in scheduled


@pytest.mark.parametrize(
    "pull_task_class",
    [
        openstack_tasks.TenantResourcesPullTask,
        openstack_tasks.TenantSubresourcesPullTask,
        openstack_tasks.TenantPropertiesPullTask,
    ],
)
def test_queued_tenant_pull_is_skipped_once_settings_are_inactive(
    dead_backend, pull_task_class
):
    """A pull queued before the settings were deactivated does not reach the backend."""
    with mock.patch.object(pull_task_class, "pull") as pull:
        pull_task_class().run(core_utils.serialize_instance(dead_backend["tenant"]))

    pull.assert_not_called()


@mock.patch("waldur_openstack.executors.TenantPullQuotasExecutor.execute")
def test_quota_pull_skips_inactive_settings(execute, dead_backend, live_tenant):
    dead_backend["tenant"].state = CoreStates.OK
    dead_backend["tenant"].save(update_fields=["state"])
    live_tenant.state = CoreStates.OK
    live_tenant.save(update_fields=["state"])

    openstack_tasks.TenantPullQuotas().run()

    pulled = [call.args[0] for call in execute.call_args_list]
    assert dead_backend["tenant"] not in pulled
    assert live_tenant in pulled


@pytest.mark.parametrize("backend_id", ["", None])
@mock.patch("waldur_openstack.executors.TenantPullQuotasExecutor.execute")
def test_quota_pull_skips_tenant_without_backend_id(execute, db, backend_id):
    """Without a backend ID there is no project to open a session for."""
    tenant = openstack_factories.TenantFactory(backend_id=backend_id)

    openstack_tasks.TenantPullQuotas().run()

    assert tenant not in [call.args[0] for call in execute.call_args_list]


@pytest.mark.parametrize("task_class", TENANT_LIST_PULL_TASKS)
def test_tenant_pulls_skip_terminated_tenant(live_tenant, task_class):
    make_zombie(live_tenant, live_tenant.project)

    assert core_utils.serialize_instance(live_tenant) not in scheduled_tenants(
        task_class
    )


@mock.patch("waldur_openstack.executors.TenantPullQuotasExecutor.execute")
def test_quota_pull_skips_terminated_tenant(execute, db):
    tenant = openstack_factories.TenantFactory()
    make_zombie(tenant, tenant.project)

    openstack_tasks.TenantPullQuotas().run()

    assert tenant not in [call.args[0] for call in execute.call_args_list]


@pytest.mark.parametrize("task_class", TENANT_LIST_PULL_TASKS)
def test_terminated_resource_without_scope_does_not_stop_tenant_pulls(
    live_tenant, task_class
):
    """A NULL inside the NOT IN subquery would exclude every tenant."""
    marketplace_factories.ResourceFactory(
        content_type=ContentType.objects.get_for_model(openstack_models.Tenant),
        object_id=None,
        state=Resource.States.TERMINATED,
    )

    assert core_utils.serialize_instance(live_tenant) in scheduled_tenants(task_class)


@pytest.mark.parametrize("task_class", TENANT_LIST_PULL_TASKS)
def test_tenant_with_live_marketplace_resource_is_pulled(live_tenant, task_class):
    marketplace_factories.ResourceFactory(
        project=live_tenant.project,
        scope=live_tenant,
        state=Resource.States.OK,
    )

    assert core_utils.serialize_instance(live_tenant) in scheduled_tenants(task_class)
