from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.importers import (
    ensure_backend_resource_not_imported,
    get_offering_scope,
    import_backend_resource,
)
from waldur_openstack import executors as openstack_executors
from waldur_openstack import models as openstack_models


def import_tenant(offering, plan, project, backend_id, additional_details):
    service_settings = get_offering_scope(offering, structure_models.ServiceSettings)
    ensure_backend_resource_not_imported(
        openstack_models.Tenant.objects.filter(
            service_settings=service_settings, backend_id=backend_id
        )
    )
    backend = service_settings.get_backend()
    return import_backend_resource(
        offering,
        plan,
        lambda: backend.import_tenant(backend_id=backend_id, project=project),
        executor=openstack_executors.TenantImportExecutor,
    )


def import_instance(offering, plan, project, backend_id, additional_details):
    tenant = get_offering_scope(offering, openstack_models.Tenant)
    ensure_backend_resource_not_imported(
        openstack_models.Instance.objects.filter(tenant=tenant, backend_id=backend_id)
    )
    backend = tenant.get_backend()
    return import_backend_resource(
        offering,
        plan,
        lambda: backend.import_instance(tenant, backend_id=backend_id, project=project),
        executor=openstack_executors.InstancePullExecutor,
    )


def import_volume(offering, plan, project, backend_id, additional_details):
    tenant = get_offering_scope(offering, openstack_models.Tenant)
    ensure_backend_resource_not_imported(
        openstack_models.Volume.objects.filter(tenant=tenant, backend_id=backend_id)
    )
    backend = tenant.get_backend()
    return import_backend_resource(
        offering,
        plan,
        lambda: backend.import_volume(tenant, backend_id=backend_id, project=project),
    )
