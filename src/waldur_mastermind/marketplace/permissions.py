from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.core import exceptions as core_exceptions
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderTypes,
    ResourceStates,
)

from . import models, utils


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


def restricted_offering_roles(offering) -> list:
    """Role names an offering is restricted to (empty if unrestricted)."""
    return offering.plugin_options.get("restricted_to_roles") or []


def offering_is_restricted(offering) -> bool:
    """True if the offering limits access to specific roles via
    plugin_options['restricted_to_roles']."""
    return bool(restricted_offering_roles(offering))


def user_active_role_names(user) -> set:
    """Names of all active roles held by the user, in any scope."""
    if user.is_anonymous:
        return set()
    return set(
        UserRole.objects.filter(is_active=True, user=user).values_list(
            "role__name", flat=True
        )
    )


def user_holds_role_in_project_scope(user, project, role_names) -> bool:
    """True if the user holds one of role_names on the given project or its
    customer. An empty role_names means "no restriction" and returns True."""
    if not role_names:
        return True
    if user.is_anonymous:
        return False
    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
    return (
        UserRole.objects.filter(is_active=True, user=user, role__name__in=role_names)
        .filter(
            Q(content_type=project_ct, object_id=project.id)
            | Q(content_type=customer_ct, object_id=project.customer_id)
        )
        .exists()
    )


def user_holds_restricted_role(user, project, offering) -> bool:
    """True if the user holds one of the offering's restricted roles in the
    given project or its customer. Used for per-project order authorization."""
    return user_holds_role_in_project_scope(
        user, project, restricted_offering_roles(offering)
    )


def offering_auto_approve_roles(offering) -> list:
    """Role names whose orders skip consumer review for this offering
    (plugin_options['auto_approve_for_roles'], empty if none). Independent of
    restricted_to_roles: governs approval, not visibility/ordering."""
    return offering.plugin_options.get("auto_approve_for_roles") or []


def user_holds_restricted_role_anywhere(user, offering, held_role_names=None) -> bool:
    """True if the user holds one of the offering's restricted roles in any
    scope. Used for the coarse catalog/is_accessible visibility check. Pass
    held_role_names (from user_active_role_names) to avoid a per-offering query."""
    role_names = restricted_offering_roles(offering)
    if not role_names:
        return True
    if held_role_names is None:
        held_role_names = user_active_role_names(user)
    return bool(set(role_names) & held_role_names)


def user_can_approve_order_as_consumer(user, order: models.Order) -> bool:
    if user.is_staff:
        return True
    return has_permission(
        user, PermissionEnum.APPROVE_ORDER, order.project
    ) or has_permission(user, PermissionEnum.APPROVE_ORDER, order.project.customer)


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
    # UNLESS the offering has disabled auto-approval
    if (
        order.offering.shared
        and order.offering.customer == order.project.customer
        and order.offering.plugin_options.get(
            "auto_approve_in_service_provider_projects"
        )
        is True
        and not order.offering.plugin_options.get("disable_autoapprove", False)
    ):
        return True

    # Service provider is not required to approve termination order
    if (
        order.type == OrderTypes.TERMINATE
        and order.offering.customer
        and structure_permissions._has_owner_access(user, order.offering.customer)
    ):
        return True

    # Skip consumer review when the offering designates the creator's role for
    # auto-approval, held on the target project or its customer. Independent of
    # the ORDER.APPROVE permission below and configurable per offering by staff.
    auto_approve_roles = offering_auto_approve_roles(order.offering)
    if auto_approve_roles and user_holds_role_in_project_scope(
        user, order.project, auto_approve_roles
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


def validate_resource_terminate_state(resource: models.Resource) -> None:
    """Allow terminate on OK/ERRED resources and on TERMINATING with pending approval."""
    if resource.state in (ResourceStates.OK, ResourceStates.ERRED):
        return
    if (
        resource.state == ResourceStates.TERMINATING
        and utils.get_pending_consumer_terminate_order(resource)
    ):
        return

    states_names = dict(ResourceStates.CHOICES)
    ok_or_erred = ", ".join(
        str(states_names[state]) for state in (ResourceStates.OK, ResourceStates.ERRED)
    )
    terminating_pending = str(states_names[ResourceStates.TERMINATING])
    raise core_exceptions.IncorrectStateException(
        _(
            "Valid states for operation: %(ok_or_erred)s, or %(terminating)s "
            "when a termination order is pending consumer approval."
        )
        % {"ok_or_erred": ok_or_erred, "terminating": terminating_pending}
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


user_can_set_end_date_as_consumer = permission_factory(
    PermissionEnum.SET_RESOURCE_END_DATE,
    ["project.customer", "project"],
)


user_can_set_end_date_as_provider = permission_factory(
    PermissionEnum.SET_RESOURCE_END_DATE,
    ["offering.customer", "offering"],
)


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
        OfferingStates.UNAVAILABLE,
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

    consent = models.UserOfferingConsent.objects.filter(
        user=user,
        offering=offering,
        revocation_date__isnull=True,
    ).first()

    if not consent:
        raise exceptions.PermissionDenied(
            f"Terms of Service consent required for offering '{offering.name}'. "
            f"Please accept the Terms of Service before accessing this resource."
        )

    # If grace period has expired and consent version doesn't match,
    active_tos = offering.terms_of_service_configs.filter(is_active=True).first()
    if (
        active_tos
        and active_tos.requires_reconsent
        and not active_tos.is_grace_period_active()
        and consent.version != active_tos.version
    ):
        raise exceptions.PermissionDenied(
            f"Your Terms of Service consent for '{offering.name}' has expired. "
            f"Please accept the updated Terms of Service (version {active_tos.version}) "
        )


def can_manage_tag(request, view, tag=None):
    """
    Check if user can update/delete a tag.
    Staff can manage any tag.
    Service providers can only manage tags they created.
    """
    if not tag:
        return

    user = request.user

    # Staff can manage any tag
    if user.is_staff:
        return

    # Creator can manage their own tag
    if tag.created_by and tag.created_by == user:
        return

    raise exceptions.PermissionDenied(_("You can only modify tags you created."))


def is_service_provider_or_staff(request, view, obj=None):
    """
    Check if user is staff or has service provider role.
    Used for tag creation - only service providers and staff can create tags.
    """
    from waldur_core.structure.managers import get_connected_customers

    user = request.user

    if user.is_staff:
        return

    # Check if user belongs to any service provider organization
    connected_customers = get_connected_customers(user)
    if models.ServiceProvider.objects.filter(
        customer_id__in=connected_customers,
    ).exists():
        return

    raise exceptions.PermissionDenied(
        _("Only staff and service providers can create tags.")
    )


def markdown_image_upload_is_enabled(request, view, obj=None):
    if not config.ENABLE_MARKDOWN_IMAGE_UPLOAD:
        raise exceptions.PermissionDenied(_("Markdown image upload is disabled."))


def can_manage_offering_lifecycle(request, view, obj=None):
    """Check if non-staff users are allowed to manage offering lifecycle.

    When ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT is False,
    only staff can perform offering lifecycle operations.
    """
    if request.user.is_staff:
        return
    if not config.ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT:
        raise exceptions.PermissionDenied()
