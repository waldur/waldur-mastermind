"""
Service functions for user data access visibility.

This module provides functions to determine who has access to a user's personal data,
including administrative access (staff/support), organizational access (same customer/project),
and service provider access (via consent).
"""

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)


def get_user_data_access_visibility(target_user, include_admin_details=False):
    """
    Get comprehensive data access visibility for a user.

    Args:
        target_user: The user whose data access to check
        include_admin_details: If True (staff/support), include individual admin user lists,
                               counts, and provider team members. If False (regular user),
                               only show description for admins (no counts).

    Returns:
        dict with administrative_access, organizational_access, service_provider_access, summary
    """
    admin_access = get_administrative_accessors(include_details=include_admin_details)
    org_access = get_organizational_accessors(target_user)
    provider_access = get_service_provider_accessors(
        target_user, include_team=include_admin_details
    )

    # Calculate admin total only if details are included (staff/support viewer)
    total_admin = None
    if include_admin_details:
        total_admin = admin_access.get("staff_count", 0) + admin_access.get(
            "support_count", 0
        )

    return {
        "administrative_access": admin_access,
        "organizational_access": org_access,
        "service_provider_access": provider_access,
        "summary": {
            "total_administrative_access": total_admin,
            "total_organizational_access": sum(
                len(scope.get("users", [])) for scope in org_access
            ),
            "total_provider_access": len(provider_access),
        },
    }


def get_administrative_accessors(include_details=False):
    """
    Get information about staff/support users with global data access.

    Args:
        include_details: If True (staff/support viewer), include counts and list of
                        individual admin/support users. If False (regular user),
                        only return description.

    Returns:
        dict with description, and optionally staff_count, support_count, users list
    """
    result = {
        "description": "Platform administrators with global access to all user data",
    }

    if include_details:
        staff_users = User.objects.filter(is_staff=True, is_active=True)
        support_users = User.objects.filter(is_support=True, is_active=True)

        result["staff_count"] = staff_users.count()
        result["support_count"] = support_users.count()

        # Combine staff and support, avoiding duplicates (staff who are also support)
        all_admin_users = User.objects.filter(
            Q(is_staff=True) | Q(is_support=True), is_active=True
        ).distinct()

        result["users"] = [
            {
                "user_uuid": str(user.uuid),
                "username": user.username,
                "full_name": user.full_name,
                "access_type": _get_admin_access_type(user),
            }
            for user in all_admin_users
        ]

    return result


def _get_admin_access_type(user):
    """Determine admin access type for a user."""
    if user.is_staff and user.is_support:
        return "staff_and_support"
    elif user.is_staff:
        return "staff"
    else:
        return "support"


def get_organizational_accessors(target_user):
    """
    Get users in the same customers/projects who can see the target user.

    All users can see individual organizational members (peer visibility).

    Args:
        target_user: The user whose organizational access to check

    Returns:
        list of dicts with scope info and users list
    """
    result = []

    # Get customers where target user has a role
    customer_ctype = ContentType.objects.get_for_model(structure_models.Customer)
    customer_ids = get_connected_customers(target_user)

    for customer_id in customer_ids:
        try:
            customer = structure_models.Customer.objects.get(id=customer_id)
        except structure_models.Customer.DoesNotExist:
            continue

        # Get users in this customer with their roles
        users_with_roles = _get_users_with_roles_in_scope(
            customer_ctype, customer_id, exclude_user=target_user
        )

        if users_with_roles:
            result.append(
                {
                    "scope_type": "customer",
                    "scope_uuid": str(customer.uuid),
                    "scope_name": customer.name,
                    "users": users_with_roles,
                }
            )

    # Get projects where target user has a role
    project_ctype = ContentType.objects.get_for_model(structure_models.Project)
    project_ids = get_connected_projects(target_user)

    for project_id in project_ids:
        try:
            project = structure_models.Project.available_objects.get(id=project_id)
        except structure_models.Project.DoesNotExist:
            continue

        # Get users in this project with their roles
        users_with_roles = _get_users_with_roles_in_scope(
            project_ctype, project_id, exclude_user=target_user
        )

        if users_with_roles:
            result.append(
                {
                    "scope_type": "project",
                    "scope_uuid": str(project.uuid),
                    "scope_name": project.name,
                    "users": users_with_roles,
                }
            )

    return result


def _get_users_with_roles_in_scope(content_type, scope_id, exclude_user=None):
    """
    Get users with their roles in a specific scope.

    Args:
        content_type: ContentType of the scope (Customer or Project)
        scope_id: ID of the scope
        exclude_user: User to exclude from the list (typically the target user)

    Returns:
        list of dicts with user_uuid, username, full_name, role
    """
    user_roles = UserRole.objects.filter(
        content_type=content_type,
        object_id=scope_id,
        is_active=True,
    ).select_related("user", "role")

    if exclude_user:
        user_roles = user_roles.exclude(user=exclude_user)

    result = []
    seen_users = set()

    for user_role in user_roles:
        user = user_role.user
        if user.id in seen_users or not user.is_active:
            continue

        seen_users.add(user.id)
        result.append(
            {
                "user_uuid": str(user.uuid),
                "username": user.username,
                "full_name": user.full_name,
                "role": user_role.role.name if user_role.role else None,
            }
        )

    return result


def get_service_provider_accessors(target_user, include_team=False):
    """
    Get offerings where the user has active consent for data sharing.

    Args:
        target_user: The user whose consents to check
        include_team: If True, include provider team members (for staff/support)

    Returns:
        list of dicts with offering info, exposed fields, and optionally provider team
    """
    # Import here to avoid circular imports
    from waldur_mastermind.marketplace.models import (
        OfferingUserAttributeConfig,
        UserOfferingConsent,
    )

    # Get active consents (not revoked)
    consents = UserOfferingConsent.objects.filter(
        user=target_user,
        revocation_date__isnull=True,
    ).select_related("offering", "offering__customer")

    result = []

    for consent in consents:
        offering = consent.offering

        # Get exposed fields for this offering
        exposed_fields = OfferingUserAttributeConfig.get_exposed_fields_for_offering(
            offering
        )

        provider_data = {
            "offering_uuid": str(offering.uuid),
            "offering_name": offering.name,
            "provider_name": offering.customer.name if offering.customer else None,
            "provider_uuid": str(offering.customer.uuid) if offering.customer else None,
            "exposed_fields": exposed_fields,
            "consent_date": consent.agreement_date.isoformat()
            if consent.agreement_date
            else None,
            "consent_version": consent.version,
        }

        if include_team and offering.customer:
            # Get users in the provider organization (customer)
            provider_data["provider_team"] = _get_provider_team(offering.customer)

        result.append(provider_data)

    return result


def _get_provider_team(customer):
    """
    Get team members of a service provider organization.

    Args:
        customer: The Customer (service provider organization)

    Returns:
        list of dicts with user info and roles
    """
    customer_ctype = ContentType.objects.get_for_model(structure_models.Customer)

    user_roles = UserRole.objects.filter(
        content_type=customer_ctype,
        object_id=customer.id,
        is_active=True,
    ).select_related("user", "role")

    result = []
    seen_users = set()

    for user_role in user_roles:
        user = user_role.user
        if user.id in seen_users or not user.is_active:
            continue

        seen_users.add(user.id)
        result.append(
            {
                "user_uuid": str(user.uuid),
                "username": user.username,
                "full_name": user.full_name,
                "role": user_role.role.name if user_role.role else None,
            }
        )

    return result
