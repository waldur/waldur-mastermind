from constance import config
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace.enums import OfferingStates, OrderTypes

from . import models


def can_register_service_provider(request, customer):
    if request.user.is_staff:
        return

    if has_permission(request, PermissionEnum.REGISTER_SERVICE_PROVIDER, customer):
        return

    raise exceptions.PermissionDenied()


def has_project_permission(request, permission, project):
    return has_permission(request, permission, project) or has_permission(
        request, permission, project.customer
    )


def order_should_not_be_reviewed_by_consumer(order: models.Order):
    # Check if purchase order upload is required and attachment is missing
    if (
        order.offering.plugin_options.get("require_purchase_order_upload", False)
        and not order.attachment
    ):
        return False

    user = order.created_by
    if user.is_staff:
        return True

    # Skip approval of private offering for project users
    if order.offering.is_private:
        return has_project_permission(
            user, PermissionEnum.APPROVE_PRIVATE_ORDER, order.project
        )

    # Skip approval of public offering belonging to the same organization under which the request is done
    if (
        order.offering.shared
        and order.offering.customer == order.project.customer
        and order.offering.plugin_options.get(
            "auto_approve_in_service_provider_projects"
        )
        is True
    ):
        return True

    # Service provider is not required to approve termination order
    if (
        order.type == OrderTypes.TERMINATE
        and order.offering.customer
        and structure_permissions._has_owner_access(user, order.offering.customer)
    ):
        return True

    return has_project_permission(user, PermissionEnum.APPROVE_ORDER, order.project)


def user_can_reject_order_as_consumer(request, view, order=None):
    if not order:
        return

    user = request.user

    if user == order.created_by:
        return

    if has_project_permission(request, PermissionEnum.REJECT_ORDER, order.project):
        return

    raise exceptions.PermissionDenied()


def user_can_list_importable_resources(
    request, view, offering: models.Offering | None = None
):
    if not offering:
        return

    user = request.user
    if user.is_staff:
        return

    if offering.shared:
        raise exceptions.PermissionDenied(
            "Import is limited to staff for shared offerings."
        )

    # Import private offerings must be available for admins and managers
    if offering.project and has_permission(
        request, PermissionEnum.LIST_IMPORTABLE_RESOURCES, offering.project
    ):
        return

    if not has_permission(
        request, PermissionEnum.LIST_IMPORTABLE_RESOURCES, offering.customer
    ):
        raise exceptions.PermissionDenied()


# Project manager/admin and customer owner are allowed to terminate resource.
# Service provider is allowed to terminate resource too.
user_can_terminate_resource = permission_factory(
    PermissionEnum.TERMINATE_RESOURCE,
    ["project", "project.customer", "offering.customer"],
)

user_can_manage_offering_user_group = permission_factory(
    PermissionEnum.MANAGE_OFFERING_USER_GROUP,
    ["offering.customer"],
)


def user_can_set_end_date_by_provider(
    request, view, obj: models.Resource | None = None
):
    if not obj:
        return
    if request.user.is_support:
        return
    if has_permission(
        request, PermissionEnum.SET_RESOURCE_END_DATE, obj.offering.customer
    ):
        return
    raise exceptions.PermissionDenied()


def user_can_update_thumbnail(request, view, obj: models.Offering | None = None):
    if not obj:
        return

    offering = obj

    if request.user.is_staff:
        return

    if offering.state not in (
        OfferingStates.ACTIVE,
        OfferingStates.DRAFT,
        OfferingStates.PAUSED,
    ):
        raise exceptions.PermissionDenied(_("You are not allowed to update a logo."))
    else:
        if has_permission(
            request, PermissionEnum.UPDATE_OFFERING_THUMBNAIL, offering.customer
        ):
            return

    raise exceptions.PermissionDenied()


def can_see_secret_options(request, instance):
    user = None
    try:
        user = request.user
        if user.is_anonymous:
            return

    except (KeyError, AttributeError):
        pass

    if isinstance(instance, list):
        offering = instance[0]
    else:
        offering = instance

    return (
        offering
        and user
        and (
            structure_permissions._has_owner_access(user, offering.customer)
            or (
                structure_permissions._has_service_manager_access(
                    user, offering.customer
                )
                and offering.customer.has_user(user)
            )
        )
    )


def check_tos_consent_permission(request, view, obj=None):
    """
    Check if user has consented to Terms of Service for the resource's offering.
    """
    if not obj:
        return

    if not config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
        return

    user = request.user
    offering = obj.offering
    if user.is_staff or user.is_support:
        return

    if not offering.plugin_options.get(
        "service_provider_can_create_offering_user", False
    ):
        return

    if not offering.has_terms_of_service():
        return

    consent_exists = models.UserOfferingConsent.objects.filter(
        user=user,
        offering=offering,
        revocation_date__isnull=True,
    ).exists()
    if not consent_exists:
        raise exceptions.PermissionDenied(
            f"Terms of Service consent required for offering '{offering.name}'. "
            f"Please accept the Terms of Service before accessing this resource."
        )
