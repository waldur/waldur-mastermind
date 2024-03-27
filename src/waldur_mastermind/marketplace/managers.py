from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db.models import Q

from waldur_core.core import managers as core_managers
from waldur_core.core.utils import is_uuid_like
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
    get_organization_groups,
)

from . import models

User = get_user_model()


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

    def filter_by_ordering_availability_for_user(self, user):
        """Returns offerings available to the user to create an order"""

        queryset = self.filter(
            state__in=[
                self.model.States.ACTIVE,
                self.model.States.PAUSED,
            ]
        )

        if user.is_anonymous:
            if not settings.WALDUR_MARKETPLACE["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"]:
                return self.none()
            else:
                return queryset.filter(shared=True)

        if user.is_staff or user.is_support:
            plans = models.Plan.objects.filter(archived=False)
            return queryset.filter(
                Q(shared=True) | Q(plans__in=plans) | Q(parent__plans__in=plans)
            ).distinct()

        # filtering by available plans
        plans = models.Plan.objects.filter(
            Q(organization_groups__isnull=True)
            | Q(organization_groups__in=get_organization_groups(user))
        ).filter(archived=False)

        # filtering by customers and projects
        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)
        connected_offerings = get_connected_offerings(user)

        return queryset.filter(
            Q(shared=True)
            | (
                (
                    Q(customer__in=connected_customers)
                    | Q(project__in=connected_projects)
                    | Q(id__in=connected_offerings)
                )
                & (Q(plans__in=plans) | Q(parent__plans__in=plans))
            )
        ).distinct()

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
                organization_groups=customer.organization_group,
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


class ResourceQuerySet(django_models.QuerySet):
    def filter_for_user(self, user):
        """
        Resources are available to both service provider and service consumer.
        """
        if user.is_anonymous or user.is_staff or user.is_support:
            return self

        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)

        return self.filter(
            Q(project__in=connected_projects)
            | Q(project__customer__in=connected_customers)
            | Q(offering__customer__in=connected_customers)
        ).distinct()


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
                organization_groups=customer.organization_group,
            )
        )

    # TODO: Remove after migration of clients to a new endpoint
    def filter_by_plan_availability_for_user(self, user):
        queryset = self.filter(
            offering__state__in=(
                models.Offering.States.ACTIVE,
                models.Offering.States.PAUSED,
            ),
            archived=False,
        )

        if user.is_anonymous:
            if not settings.WALDUR_MARKETPLACE["ANONYMOUS_USER_CAN_VIEW_PLANS"]:
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


def filter_offering_permissions(user, is_active=True):
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
