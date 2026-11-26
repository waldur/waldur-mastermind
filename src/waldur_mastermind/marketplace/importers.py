from collections.abc import Callable

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_core.structure import signals as structure_signals
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.marketplace import models


def get_offering_scope[T](offering: models.Offering, expected_type: type[T]) -> T:
    scope = offering.scope
    if not isinstance(scope, expected_type):
        raise rf_exceptions.ValidationError(
            _("Offering does not have a valid scope for resource import.")
        )
    return scope


def ensure_backend_resource_not_imported(queryset) -> None:
    if queryset.exists():
        raise rf_exceptions.ValidationError(_("Resource has been imported already."))


def import_backend_resource(
    offering: models.Offering,
    plan: models.Plan | None,
    import_backend: Callable,
    executor=None,
) -> models.Resource:
    """Shared tail of the plugin resource importers.

    ``import_backend`` creates the plugin's own resource from the backend. The
    ``resource_imported`` handler of the plugin then creates the marketplace
    resource, which is returned.
    """
    try:
        resource = import_backend()
    except ServiceBackendError as e:
        raise rf_exceptions.ValidationError(str(e))

    structure_signals.resource_imported.send(
        sender=resource.__class__,
        instance=resource,
        plan=plan,
        offering=offering,
    )

    marketplace_resource = models.Resource.objects.get(scope=resource)

    if executor:
        transaction.on_commit(lambda: executor.execute(resource))

    return marketplace_resource
