from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db.models import Q

from waldur_core.core import managers as core_managers
from waldur_core.core.models import User
from waldur_core.core.utils import is_uuid_like
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_customers_by_permission,
    get_connected_projects,
    get_connected_projects_by_permission,
    get_organization_groups,
)
from waldur_mastermind.marketplace.enums import OfferingStates

from . import models


class MixinManager(core_managers.GenericKeyMixin, django_models.Manager):
    pass


class OfferingQuerySet(django_models.QuerySet):
    def filter_for_user(self, user):
        """Returns offerings related to user."""

        if user.is_anonymous:
            return self.none()

        if user.is_staff or user.is_support:
            return self

        connected_customers = get_connected_customers(user)
        connected_projects = get_connected_projects(user)
        connected_offerings = get_connected_offerings(user)

        return self.filter(
            Q(customer__in=connected_customers)
            | Q(project__in=connected_projects)
            | Q(id__in=connected_offerings)
        ).distinct()

    @staticmethod
    def _restricted_forbidden_ids(queryset, user):
        """Ids of offerings in queryset that restrict access via
        plugin_options['restricted_to_roles'] to roles the user does not hold.
        The check is coarse (role held in any scope); precise per-project
        authorization happens at order creation."""
        restricted = queryset.filter(
            plugin_options__has_key="restricted_to_roles"
        ).values_list("id", "plugin_options")

        if user.is_anonymous:
            user_role_names = set()
        else:
            user_role_names = set(
                UserRole.objects.filter(is_active=True, user=user).values_list(
                    "role__name", flat=True
                )
            )

        return {
            offering_id
            for offering_id, plugin_options in restricted
            if plugin_options.get("restricted_to_roles")
            and not set(plugin_options["restricted_to_roles"]) & user_role_names
        }

    def _exclude_restricted_offerings(self, queryset, user):
        """Hide offerings that restrict access via
        plugin_options['restricted_to_roles'] from users who do not hold one of
        the listed roles, except offerings the user already consumes resources
        from. Catalog visibility is coarse (role held in any scope); precise
        per-project authorization happens at order creation."""
        forbidden_ids = self._restricted_forbidden_ids(queryset, user)
        if not forbidden_ids:
            return queryset
        if not user.is_anonymous:
            # Keep offerings the user already consumes resources from.
            connected = models.Resource.objects.filter(
                project__in=get_connected_projects(user),
                offering_id__in=forbidden_ids,
            ).values_list("offering_id", flat=True)
            forbidden_ids -= set(connected)
        return queryset.exclude(id__in=forbidden_ids)

    def filter_accessible_for_user(self, user):
        """Drop restricted offerings the user is not allowed to order.

        Unlike the catalog default (filter_by_ordering_availability_for_user),
        this does NOT keep offerings the user merely consumes a resource from:
        it returns only offerings the user could actually order. Backs the
        `accessible` query filter so the marketplace catalog can request only
        orderable offerings while resource-driven detail/retrieve keeps showing
        the rest."""
        return self.exclude(id__in=self._restricted_forbidden_ids(self, user))

    def filter_by_ordering_availability_for_user(self, user):
        """Returns offerings available to the user to create an order"""

        queryset = self.filter(state__in=[OfferingStates.ACTIVE, OfferingStates.PAUSED])

        if user.is_anonymous:
            if not config.ANONYMOUS_USER_CAN_VIEW_OFFERINGS:
                return self.none()
            else:
                return self._exclude_restricted_offerings(
                    queryset.filter(shared=True), user
                )

        # Staff/support ALWAYS see all offerings regardless of visibility setting
        if user.is_staff or user.is_support:
            plans = models.Plan.objects.filter(archived=False)
            return queryset.filter(
                Q(shared=True) | Q(plans__in=plans) | Q(parent__plans__in=plans)
            ).distinct()

        # Get user's organization groups
        user_organization_groups = get_organization_groups(user)

        # Filter plans by user's organization groups
        accessible_plans = models.Plan.objects.filter(
            Q(organization_groups__isnull=True)
            | Q(organization_groups__in=user_organization_groups)
        ).filter(archived=False)

        # Get user connections
        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)
        connected_offerings = get_connected_offerings(user)

        visibility_mode = getattr(
            config, "RESTRICTED_OFFERING_VISIBILITY_MODE", "show_all"
        )

        # require_membership: user must belong to at least one org/project
        if visibility_mode == "require_membership":
            has_membership = (
                connected_customers.exists()
                or connected_projects.exists()
                or connected_offerings.exists()
            )
            if not has_membership:
                return self.none()
            # Fall through to hide_inaccessible logic for members
            visibility_mode = "hide_inaccessible"

        if visibility_mode == "hide_inaccessible":
            # Shared offerings: must match org groups AND have accessible plans
            shared_filter = (
                Q(shared=True, organization_groups__isnull=True)
                | Q(shared=True, organization_groups__in=user_organization_groups)
            ) & (Q(plans__in=accessible_plans) | Q(parent__plans__in=accessible_plans))

            # Private offerings: user connected AND has plan access
            private_filter = (
                Q(customer__in=connected_customers)
                | Q(project__in=connected_projects)
                | Q(id__in=connected_offerings)
            ) & (Q(plans__in=accessible_plans) | Q(parent__plans__in=accessible_plans))

            return self._exclude_restricted_offerings(
                queryset.filter(shared_filter | private_filter).distinct(), user
            )
        else:
            # "show_all" or "show_restricted_disabled" - return all shared offerings
            # (show_restricted_disabled is handled by frontend marking)
            return self._exclude_restricted_offerings(
                queryset.filter(
                    Q(shared=True)
                    | (
                        (
                            Q(customer__in=connected_customers)
                            | Q(project__in=connected_projects)
                            | Q(id__in=connected_offerings)
                        )
                        & (
                            Q(plans__in=accessible_plans)
                            | Q(parent__plans__in=accessible_plans)
                        )
                    )
                ).distinct(),
                user,
            )

    def filter_for_customer(self, value):
        if not is_uuid_like(value):
            return self.none()
        try:
            customer = structure_models.Customer.objects.get(uuid=value)
        except structure_models.Customer.DoesNotExist:
            return self.none()

        return self.filter(
            Q(shared=True, organization_groups__isnull=True)
            | Q(
                shared=True,
                organization_groups__isnull=False,
                organization_groups__in=customer.organization_groups.all(),
            )
            | Q(customer__uuid=value)
        )

    def filter_for_service_manager(self, value):
        if not is_uuid_like(value):
            return self.none()

        try:
            user = User.objects.get(uuid=value)
        except User.DoesNotExist:
            return self.none()

        return self.filter(shared=True, id__in=get_connected_offerings(user))

    def filter_for_project(self, value):
        if not is_uuid_like(value):
            return self.none()
        return self.filter(Q(shared=True) | Q(project__uuid=value))

    def filter_importable(self, user):
        # Import is limited to staff for shared offerings and to staff/owners for private offerings

        if user.is_staff:
            return self

        return self.filter(
            shared=False,
            customer__in=get_connected_customers(user, RoleEnum.CUSTOMER_OWNER),
        )


class OfferingManager(MixinManager):
    def get_queryset(self):
        return OfferingQuerySet(self.model, using=self._db)


class ResourceQuerySet(django_models.QuerySet["models.Resource"]):
    def filter_for_service_consumer(self, user):
        if user.is_anonymous or user.is_staff or user.is_support:
            return self

        connected_projects = get_connected_projects_by_permission(
            user, PermissionEnum.LIST_RESOURCES
        )
        connected_customers = get_connected_customers_by_permission(
            user, PermissionEnum.LIST_RESOURCES
        )
        # Direct UserRole on Resource or ResourceProject grants read-only
        # visibility of the parent Resource regardless of the LIST_RESOURCES
        # permission on the project / customer chain.
        direct_resource_ids = get_user_direct_resource_ids(user)
        rp_resource_ids = get_user_resource_project_resource_ids(user)
        return self.filter(
            Q(project__in=connected_projects)
            | Q(project__customer__in=connected_customers)
            | Q(id__in=direct_resource_ids)
            | Q(id__in=rp_resource_ids)
        ).distinct()

    def filter_for_service_provider(self, user):
        if user.is_staff or user.is_support:
            return self

        connected_customers = get_connected_customers(user)
        connected_service_providers = get_connected_serviceproviders(user)
        connected_offerings = get_connected_offerings(user)

        return self.filter(
            Q(offering__customer__in=connected_customers)
            | Q(offering__customer__serviceprovider__in=connected_service_providers)
            | Q(offering__in=connected_offerings)
        ).distinct()

    def filter_for_user(self, user):
        if user.is_staff or user.is_support:
            return self
        direct_resource_ids = get_user_direct_resource_ids(user)
        rp_resource_ids = get_user_resource_project_resource_ids(user)
        return self.filter(
            Q(project__in=get_connected_projects(user))
            | Q(project__customer__in=get_connected_customers(user))
            | Q(id__in=direct_resource_ids)
            | Q(id__in=rp_resource_ids)
        ).distinct()


def get_user_direct_resource_ids(user):
    """IDs of Resources where the user has a direct UserRole."""
    resource_ct = ContentType.objects.get_for_model(models.Resource)
    return get_scope_ids(user, resource_ct)


def get_user_resource_project_ids(user):
    """IDs of ResourceProjects where the user has a direct UserRole."""
    rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
    return get_scope_ids(user, rp_ct)


def get_user_resource_project_resource_ids(user):
    """IDs of Resources whose ResourceProjects the user has a UserRole on."""
    return models.ResourceProject.available_objects.filter(
        id__in=get_user_resource_project_ids(user)
    ).values_list("resource_id", flat=True)


def _user_resource_descended_resource_ids_qs(user):
    """Lazy QuerySet of Resource IDs reachable via the user's Resource OR
    ResourceProject UserRoles. Filters by content-type app_label/model
    directly to avoid a per-call ``ContentType.objects.get_for_model`` round
    trip — the join folds into the outer SQL as a subquery."""
    direct_role_ids = UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type__app_label="marketplace",
        content_type__model="resource",
    ).values_list("object_id", flat=True)
    rp_role_ids = UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type__app_label="marketplace",
        content_type__model="resourceproject",
    ).values_list("object_id", flat=True)
    rp_resource_ids = models.ResourceProject.available_objects.filter(
        id__in=rp_role_ids
    ).values_list("resource_id", flat=True)
    return models.Resource.objects.filter(
        Q(id__in=direct_role_ids) | Q(id__in=rp_resource_ids)
    )


def get_user_resource_descended_project_ids(user):
    """Lazy QuerySet of structure Project IDs reachable via the user's
    Resource or ResourceProject UserRoles. Returned as a single QuerySet so
    callers can fold it into ``Q(id__in=...)`` as a SQL subquery without
    forcing evaluation."""
    return _user_resource_descended_resource_ids_qs(user).values_list(
        "project_id", flat=True
    )


def get_user_resource_descended_customer_ids(user):
    """Lazy QuerySet of Customer IDs reachable via the user's Resource or
    ResourceProject UserRoles. See ``get_user_resource_descended_project_ids``."""
    return _user_resource_descended_resource_ids_qs(user).values_list(
        "project__customer_id", flat=True
    )


class ResourceManager(MixinManager):
    def get_queryset(self):
        return ResourceQuerySet(self.model, using=self._db)


class PlanQuerySet(django_models.QuerySet):
    def filter_for_customer(self, value):
        customer = structure_models.Customer.objects.get(uuid=value)
        return self.filter(
            Q(organization_groups__isnull=True)
            | Q(
                organization_groups__isnull=False,
                organization_groups__in=customer.organization_groups.all(),
            )
        )

    # TODO: Remove after migration of clients to a new endpoint
    def filter_by_plan_availability_for_user(self, user):
        queryset = self.filter(
            offering__state__in=(
                OfferingStates.ACTIVE,
                OfferingStates.PAUSED,
            ),
            archived=False,
        )

        if user.is_anonymous:
            if not config.ANONYMOUS_USER_CAN_VIEW_PLANS:
                return self.none()
            else:
                return queryset.filter(offering__shared=True)

        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)
        connected_offerings = get_connected_offerings(user)

        q1 = Q(organization_groups__isnull=True) | Q(
            organization_groups__in=get_organization_groups(user)
        )
        q2 = (
            Q(offering__customer__in=connected_customers)
            | Q(offering__project__in=connected_projects)
            | Q(offering__in=connected_offerings)
        )
        q3 = Q(offering__shared=True)
        return queryset.filter(q3 | (q2 & q1)).distinct()


class PlanManager(MixinManager):
    def get_queryset(self):
        return PlanQuerySet(self.model, using=self._db)


def get_connected_offerings(user, role=None):
    content_type = ContentType.objects.get_for_model(models.Offering)
    return get_scope_ids(user, content_type, role)


def get_connected_offerings_by_permission(user, permission):
    from waldur_core.permissions.models import Role

    content_type = ContentType.objects.get_for_model(models.Offering)
    roles = list(
        Role.objects.filter(
            content_type=content_type,
            is_active=True,
            permissions__permission=permission,
        ).values_list("name", flat=True)
    )
    if not roles:
        return models.Offering.objects.none().values_list("id", flat=True)
    return get_connected_offerings(user, roles)


def get_connected_serviceproviders(user, role=None):
    content_type = ContentType.objects.get_for_model(models.ServiceProvider)
    return get_scope_ids(user, content_type, role)


def filter_offering_permissions(user, is_active=True):
    if user.is_anonymous:
        return UserRole.objects.none()

    queryset = UserRole.objects.filter(
        content_type=ContentType.objects.get_for_model(models.Offering),
        role__name=RoleEnum.OFFERING_MANAGER,
        is_active=is_active,
    ).order_by("-created")

    if not (user.is_staff or user.is_support):
        visible_offerings = models.Offering.objects.filter(
            customer__in=get_connected_customers(user)
        )
        queryset = queryset.filter(
            Q(user=user) | Q(object_id__in=visible_offerings)
        ).distinct()

    return queryset
