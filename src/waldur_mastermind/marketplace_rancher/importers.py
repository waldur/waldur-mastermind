from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.importers import (
    ensure_backend_resource_not_imported,
    get_offering_scope,
    import_backend_resource,
)
from waldur_mastermind.marketplace_rancher import executors
from waldur_rancher import models as rancher_models
from waldur_rancher.exceptions import RancherException


def import_cluster(offering, plan, project, backend_id, additional_details):
    service_settings = get_offering_scope(offering, structure_models.ServiceSettings)
    if not isinstance(additional_details, dict):
        raise rf_exceptions.ValidationError(
            _("additional_details must be a dictionary.")
        )
    ensure_backend_resource_not_imported(
        rancher_models.Cluster.objects.filter(
            service_settings=service_settings, backend_id=backend_id
        )
    )
    backend = service_settings.get_backend()
    tenant_uuid = additional_details.get("tenant_uuid")

    def _import_cluster():
        try:
            return backend.import_cluster(
                backend_id=backend_id,
                project=project,
                tenant_uuid=tenant_uuid,
            )
        except RancherException as e:
            raise rf_exceptions.ValidationError(str(e))

    return import_backend_resource(
        offering,
        plan,
        _import_cluster,
        executor=executors.ManagedRancherImportExecutor,
    )
