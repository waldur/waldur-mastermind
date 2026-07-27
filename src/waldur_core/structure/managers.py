from datetime import timedelta
from typing import TypeVar

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import F, Model, Q, QuerySet
from django.db.models.functions import Now
from rest_framework.authtoken import models as authtoken_models

from waldur_core.core import utils as core_utils
from waldur_core.core.managers import GenericKeyMixin
from waldur_core.core.models import User
from waldur_core.permissions.mixins import PermissionMixin
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import get_scope_ids, get_user_ids
from waldur_core.structure import models as structure_models


def build_filter(path, ids):
    if path == "self":
        path = "id"
    return models.Q(**{f"{path}__in": ids})


T = TypeVar("T", bound=Model)


def filter_queryset_for_user[T: Model](
    queryset: QuerySet[T], user: User
) -> QuerySet[T]:
    def get_customer_subquery(path):
        if list_permission:
            connected_customers = get_connected_customers_by_permission(
                user, list_permission
            )
        else:
            connected_customers = get_connected_customers(user)
        return build_filter(path, connected_customers)

    def get_project_subquery(path):
        if list_permission:
            connected_projects = get_connected_projects_by_permission(
                user, list_permission
            )
        else:
            connected_projects = get_connected_projects(user)
        return build_filter(path, connected_projects)

    if queryset is None:
        return queryset

    if user is None or not user.is_authenticated or not user.is_active:
        return queryset.none()

    if user.is_staff or user.is_support:
        return queryset

    try:
        permissions = queryset.model.Permissions
    except AttributeError:
        return queryset

    list_permission = getattr(permissions, "list_permission", None)

    subquery = models.Q()

    customer_path = getattr(permissions, "customer_path", None)
    project_path = getattr(permissions, "project_path", None)

    if customer_path:
        if isinstance(customer_path, list | tuple):
            for p in customer_path:
                subquery |= get_customer_subquery(p)
        else:
            subquery |= get_customer_subquery(customer_path)

    if project_path:
        if isinstance(project_path, list | tuple):
            for p in project_path:
                subquery |= get_project_subquery(p)
        else:
            subquery |= get_project_subquery(project_path)

    build_query = getattr(permissions, "build_query", None)
    if build_query:
        subquery |= build_query(user)

    if issubclass(queryset.model, PermissionMixin) and issubclass(
        queryset.model, Model
    ):
        content_type = ContentType.objects.get_for_model(queryset.model)
        subquery |= models.Q(id__in=get_scope_ids(user, content_type))

    # Resource and ResourceProject UserRoles confer read-only visibility of
    # the parent project / customer so role-holders can render the UI. The
    # helpers return lazy QuerySets — the lookup folds into the outer SQL
    # as a subquery without an extra round-trip.
    if queryset.model is structure_models.Project:
        rp_project_qs = _get_resource_role_project_ids_qs(user)
        if rp_project_qs is not None:
            subquery |= models.Q(id__in=rp_project_qs)
    elif queryset.model is structure_models.Customer:
        rp_customer_qs = _get_resource_role_customer_ids_qs(user)
        if rp_customer_qs is not None:
            subquery |= models.Q(id__in=rp_customer_qs)

    if not subquery:
        return queryset

    return queryset.filter(subquery).distinct()


def _get_resource_role_project_ids_qs(user):
    """Lazy QuerySet of structure Project IDs reachable via the user's
    Resource / ResourceProject UserRoles.

    Returns ``None`` when the marketplace module is not installed.
    """
    try:
        from waldur_mastermind.marketplace.managers import (
            get_user_resource_descended_project_ids,
        )
    except ImportError:
        return None
    return get_user_resource_descended_project_ids(user)


def _get_resource_role_customer_ids_qs(user):
    """Lazy QuerySet of Customer IDs reachable via the user's Resource /
    ResourceProject UserRoles. See ``_get_resource_role_project_ids_qs``."""
    try:
        from waldur_mastermind.marketplace.managers import (
            get_user_resource_descended_customer_ids,
        )
    except ImportError:
        return None
    return get_user_resource_descended_customer_ids(user)


def filter_customer_by_ip_address(ip_address):
    return structure_models.Customer.objects.filter(
        models.Q(access_subnet_set__inet__isnull=True)
        | models.Q(access_subnet_set__inet__net_contains_or_equals=ip_address)
    ).values_list("id", flat=True)


def filter_queryset_by_user_ip(queryset, request):
    user = request.user
    # Normalise the raw, client-controlled X-Forwarded-For: an unparseable
    # value must resolve to None (which the `not user_ip` guard below treats as
    # "no restriction") rather than reaching the Postgres inet lookup, where it
    # would raise and 500 the request.
    user_ip = core_utils.normalize_ip_address(core_utils.get_ip_address(request))

    if queryset is None:
        return queryset

    if user is None or not user.is_authenticated:
        return queryset.none()

    if user.is_staff or user.is_support or not user_ip:
        return queryset

    try:
        permissions = queryset.model.Permissions
    except AttributeError:
        return queryset

    customer_path = getattr(permissions, "customer_path", None)
    if not customer_path:
        return queryset

    subquery = models.Q()

    def get_subquery(c_path):
        if c_path == "self":
            path = "id__in"
            none_path = "id"
        else:
            path = c_path + "__id__in"
            none_path = c_path + "_id"

        customers_ids = filter_customer_by_ip_address(user_ip)
        return models.Q(**{path: customers_ids}) | models.Q(**{none_path: None})

    if isinstance(customer_path, list | tuple):
        for p in customer_path:
            subquery |= get_subquery(p)
    else:
        subquery |= get_subquery(customer_path)

    return queryset.filter(subquery)


class ServiceSettingsManager(GenericKeyMixin, models.Manager):
    """Allows to filter and get service settings by generic key"""

    def get_available_models(self):
        """Return list of models that are acceptable"""
        from waldur_core.structure.models import BaseResource

        return BaseResource.get_all_models()


class SharedServiceSettingsManager(ServiceSettingsManager):
    def get_queryset(self):
        return super().get_queryset().filter(shared=True)


class PrivateServiceSettingsManager(ServiceSettingsManager):
    def get_queryset(self):
        return super().get_queryset().filter(shared=False)


def get_connected_customers(user, role=None):
    ctype = ContentType.objects.get_for_model(structure_models.Customer)
    return get_scope_ids(user, ctype, role)


def get_connected_projects(user, role=None):
    ctype = ContentType.objects.get_for_model(structure_models.Project)
    return get_scope_ids(user, ctype, role)


def get_connected_customers_by_permission(user, permission):
    ctype = ContentType.objects.get_for_model(structure_models.Customer)
    roles = list(
        Role.objects.filter(
            content_type=ctype, is_active=True, permissions__permission=permission
        ).values_list("name", flat=True)
    )
    if not roles:
        return structure_models.Customer.objects.none()
    return get_connected_customers(user, roles)


def get_connected_projects_by_permission(user, permission):
    ctype = ContentType.objects.get_for_model(structure_models.Project)
    roles = list(
        Role.objects.filter(
            content_type=ctype, is_active=True, permissions__permission=permission
        ).values_list("name", flat=True)
    )
    if not roles:
        return structure_models.Project.objects.none()
    return get_connected_projects(user, roles)


def get_customer_users(scope_ids, role=None):
    ctype = ContentType.objects.get_for_model(structure_models.Customer)
    return get_user_ids(ctype, scope_ids, role)


def get_project_users(scope_ids, role=None):
    ctype = ContentType.objects.get_for_model(structure_models.Project)
    return get_user_ids(ctype, scope_ids, role)


def get_visible_users(user):
    customer_users = get_customer_users(get_visible_customers(user))
    project_users = get_project_users(get_visible_projects(user))
    return customer_users.union(project_users)


def get_nested_customer_users(customer):
    customer_users = get_customer_users(customer.id)
    project_ids = structure_models.Project.available_objects.filter(
        customer=customer
    ).values_list("id", flat=True)
    project_users = get_project_users(project_ids)
    return customer_users.union(project_users)


def count_customer_users(customer):
    return get_nested_customer_users(customer).count()


def get_visible_customers(user):
    direct_projects = get_connected_projects(user)
    direct_customers = get_connected_customers(user)
    indirect_customers = structure_models.Project.available_objects.filter(
        id__in=direct_projects
    ).values_list("customer_id", flat=True)

    from waldur_mastermind.proposal.managers import (
        get_connected_call_organizers,
        get_connected_calls,
    )
    from waldur_mastermind.proposal.models import Call, CallManagingOrganisation

    call_manager_customers = CallManagingOrganisation.objects.filter(
        id__in=get_connected_call_organizers(user)
    ).values_list("customer_id", flat=True)
    call_customers = Call.objects.filter(id__in=get_connected_calls(user)).values_list(
        "manager__customer_id", flat=True
    )
    return (
        direct_customers.union(indirect_customers)
        .union(call_manager_customers)
        .union(call_customers)
    )


def get_visible_projects(user):
    direct_customers = get_connected_customers(user)
    direct_projects = get_connected_projects(user)
    indirect_projects = structure_models.Project.available_objects.filter(
        customer_id__in=direct_customers
    ).values_list("id", flat=True)
    return direct_projects.union(indirect_projects)


def get_organization_groups(user):
    return (
        structure_models.Customer.objects.filter(id__in=get_visible_customers(user))
        .values_list("organization_groups__id", flat=True)
        .distinct()
    )


def get_active_tokens():
    # Get tokens that are either:
    # 1. Within their lifetime (created + token_lifetime > now)
    # 2. Have no lifetime limit (token_lifetime is NULL)
    return authtoken_models.Token.objects.filter(
        Q(created__gte=Now() - F("user__token_lifetime") * timedelta(seconds=1))
        | Q(user__token_lifetime__isnull=True)
    ).select_related("user")
