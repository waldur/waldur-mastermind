import datetime
import logging
from collections import defaultdict
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet, Sum
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.models import IdentityProvider
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure.signals import project_moved
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    ResourceStates,
)

logger = logging.getLogger(__name__)


def get_identity_provider_field(registration_method, field):
    try:
        return getattr(
            IdentityProvider.objects.get(provider=registration_method), field
        )
    except IdentityProvider.DoesNotExist:
        return None


def get_identity_provider_name(registration_method):
    if registration_method in ProviderChoices.CHOICES:
        return get_identity_provider_field(registration_method, "label") or ""

    if registration_method == settings.WALDUR_AUTH_SAML2["NAME"]:
        return settings.WALDUR_AUTH_SAML2["IDENTITY_PROVIDER_LABEL"]

    if registration_method == "valimo":
        return settings.WALDUR_AUTH_VALIMO["LABEL"]

    if registration_method == "default":
        return settings.WALDUR_CORE["LOCAL_IDP_NAME"]

    return ""


def get_identity_provider_label(registration_method):
    if registration_method in ProviderChoices.CHOICES:
        return get_identity_provider_field(registration_method, "label") or ""

    if registration_method == settings.WALDUR_AUTH_SAML2["NAME"]:
        return settings.WALDUR_AUTH_SAML2["IDENTITY_PROVIDER_LABEL"]

    if registration_method == "valimo":
        return settings.WALDUR_AUTH_VALIMO["LABEL"]

    if registration_method == "default":
        return settings.WALDUR_CORE["LOCAL_IDP_LABEL"]

    return ""


def get_identity_provider_management_url(registration_method):
    if registration_method in ProviderChoices.CHOICES:
        return get_identity_provider_field(registration_method, "management_url") or ""

    if registration_method == settings.WALDUR_AUTH_SAML2["NAME"]:
        return settings.WALDUR_AUTH_SAML2["MANAGEMENT_URL"]

    if registration_method == "valimo":
        return settings.WALDUR_AUTH_VALIMO["USER_MANAGEMENT_URL"]

    if registration_method == "default":
        return settings.WALDUR_CORE["LOCAL_IDP_MANAGEMENT_URL"]

    return ""


def get_identity_provider_fields(registration_method):
    if registration_method in ProviderChoices.CHOICES:
        return (
            get_identity_provider_field(registration_method, "protected_fields") or []
        )

    return {
        settings.WALDUR_AUTH_SAML2["NAME"]: [
            v[0] for v in settings.WALDUR_AUTH_SAML2["SAML_ATTRIBUTE_MAPPING"].values()
        ],
        "valimo": settings.WALDUR_AUTH_VALIMO["USER_PROTECTED_FIELDS"],
        "default": settings.WALDUR_CORE["LOCAL_IDP_PROTECTED_FIELDS"],
    }.get(registration_method, [])


def update_pulled_fields(instance, imported_instance, fields):
    """
    Update instance fields based on imported from backend data.
    Save changes to DB only one or more fields were changed.
    """
    modified = False
    for field in fields:
        pulled_value = getattr(imported_instance, field)
        current_value = getattr(instance, field)

        if field == "directly_connected_ips":
            pulled_value = ",".join(sorted(pulled_value.split(",")))
            current_value = ",".join(sorted(current_value.split(",")))

        if current_value != pulled_value:
            setattr(instance, field, pulled_value)
            logger.info(
                "%s's with PK %s %s field updated from value '%s' to value '%s'",
                instance.__class__.__name__,
                instance.pk,
                field,
                current_value,
                pulled_value,
            )
            modified = True
    error_message = getattr(imported_instance, "error_message", "") or getattr(
        instance, "error_message", ""
    )
    if error_message and instance.error_message != error_message:
        instance.error_message = imported_instance.error_message
        modified = True
    if modified:
        instance.save()
    return modified


def handle_resource_not_found(resource):
    """
    Set resource state to ERRED and append/create "not found" error message.
    """
    resource.set_erred()
    resource.runtime_state = ""
    message = "Does not exist at backend."
    if message not in resource.error_message:
        if not resource.error_message:
            resource.error_message = message
        else:
            resource.error_message += " (%s)" % message
    resource.save()
    logger.warning(
        f"{resource.__class__.__name__} {resource} (PK: {resource.pk}) does not exist at backend."
    )


def handle_resource_update_success(resource):
    """
    Recover resource if its state is ERRED and clear error message.
    """
    update_fields = []
    if resource.state == CoreStates.ERRED:
        resource.recover()
        update_fields.append("state")

    if resource.state in (CoreStates.UPDATING, CoreStates.CREATING):
        resource.set_ok()
        update_fields.append("state")

    if resource.error_message:
        resource.error_message = ""
        update_fields.append("error_message")

    if hasattr(resource, "task_id"):
        resource.task_id = None
        update_fields.append("task_id")

    if update_fields:
        resource.save(update_fields=update_fields)
    logger.info(
        f"{resource.__class__.__name__} {resource} (PK: {resource.pk}) was successfully updated."
    )


def check_customer_blocked_or_archived(obj):
    from waldur_core.structure import permissions

    customer = permissions._get_customer(obj)
    if customer and customer.blocked:
        raise ValidationError(_("Blocked organization is not available."))
    elif customer and customer.archived:
        raise ValidationError(_("Archived organization is not available."))


def project_is_empty(obj):
    from waldur_mastermind.marketplace.models import Resource

    if (
        Resource.objects.filter(project=obj)
        .exclude(state=ResourceStates.TERMINATED)
        .exists()
    ):
        raise ValidationError(
            _(
                "Project contains active resources. "
                "Please remove them before deleting project."
            )
        )


def check_project_end_date(obj):
    from waldur_core.structure import permissions

    project = permissions._get_project(obj)
    if project.is_expired:
        raise ValidationError(_("Project '%s' is expired.") % project)


@transaction.atomic
def move_project(project, customer, current_user=None, preserve_permissions=False):
    if customer.blocked:
        raise ValidationError(_("New customer must be not blocked"))

    old_customer = project.customer
    if customer == old_customer:
        raise ValidationError(_("New customer must be different than current one"))

    project.customer = customer
    project.save(update_fields=["customer"])

    if not preserve_permissions:
        for permission in get_permissions(project):
            permission.revoke(current_user)
            logger.info(f"Permission {permission} has been revoked")

    project_moved.send(
        sender=project.__class__,
        project=project,
        old_customer=old_customer,
        new_customer=customer,
    )


def get_components_usage_data_from_resources(
    resources: QuerySet[marketplace_models.Resource],
    for_current_month: bool = False,
) -> list[dict[str, Any]]:
    offerings = marketplace_models.Offering.objects.filter(
        id__in=resources.values_list("offering_id", flat=True)
    ).distinct()

    components = marketplace_models.OfferingComponent.objects.filter(
        offering__in=offerings
    ).distinct()

    component_usage = defaultdict(float)
    component_limit = defaultdict(float)
    component_limit_usage = defaultdict(float)

    current_date = datetime.date.today()

    for resource in resources:
        for component_type, usage in resource.current_usages.items():
            # When filtering for current month, get usage from ComponentUsage instead of current_usages
            if not for_current_month:
                component_usage[component_type] += float(usage)

        for component_type, limit in resource.limits.items():
            if limit is not None:
                component_limit[component_type] += float(limit)

        usage_components = resource.offering.components.filter(
            billing_type=BillingTypes.USAGE
        )
        limit_components = resource.offering.components.filter(
            billing_type=BillingTypes.LIMIT
        )

        if for_current_month:
            for component in usage_components:
                usages = marketplace_models.ComponentUsage.objects.filter(
                    resource=resource,
                    component=component,
                    date__year=current_date.year,
                    date__month=current_date.month,
                )
                total_usage = usages.aggregate(total=Sum("usage"))["total"] or 0
                component_usage[component.type] += float(total_usage)

        for component in limit_components:
            if not for_current_month and component.limit_period in (
                None,
                LimitPeriods.MONTH,
            ):
                component_limit_usage[component.type] += float(
                    resource.current_usages.get(component.type, 0)
                )
            else:
                usages = marketplace_models.ComponentUsage.objects.filter(
                    resource=resource, component=component
                ).exclude(plan_period=None)

                if for_current_month:
                    usages = usages.filter(
                        date__year=current_date.year,
                        date__month=current_date.month,
                    )
                elif component.limit_period == LimitPeriods.ANNUAL:
                    usages = usages.filter(date__year__gte=datetime.date.today().year)

                total_usage = usages.aggregate(total=Sum("usage"))["total"] or 0
                component_limit_usage[component.type] += float(total_usage)

    components_data = {}
    for component in components:
        if component.type not in components_data:
            components_data[component.type] = {
                "type": component.type,
                "name": component.name,
                "description": component.description,
                "measured_unit": component.measured_unit,
                "billing_type": component.billing_type,
                "usage": component_usage.get(component.type, 0),
                "limit_usage": component_limit_usage.get(component.type, 0),
                "limit": component_limit.get(component.type, None),
                "offering_name": component.offering.name,
                "offering_uuid": component.offering.uuid.hex,
            }

    return list(components_data.values())
