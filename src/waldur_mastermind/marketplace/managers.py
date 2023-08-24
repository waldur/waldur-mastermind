from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db.models import Q

from waldur_core.core import managers as core_managers
from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure import models as structure_models
from waldur_core.structure import utils as structure_utils

from . import models


def get_connected_customers(user):
    customer_type = ContentType.objects.get_for_model(structure_models.Customer)
    return get_scope_ids(user, customer_type)


def get_connected_projects(user):
    project_type = ContentType.objects.get_for_model(structure_models.Project)
    return get_scope_ids(user, project_type)


class MixinManager(core_managers.GenericKeyMixin, django_models.Manager):
    pass


class OfferingQuerySet(django_models.QuerySet):
    def filter_for_user(self, user, customer_roles=None, project_roles=None):
        """Returns offerings related to user."""

        if user.is_anonymous:
            return self.none()

        if user.is_staff or user.is_support:
            return self

        connected_customers = structure_models.Customer.objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )

        if customer_roles is not None:
            connected_customers = connected_customers.filter(
                permissions__role__in=customer_roles
            )

        connected_projects = structure_models.Project.available_objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )

        if project_roles is not None:
            connected_projects = connected_projects.filter(
                permissions__role__in=project_roles
            )

        return self.filter(
            Q(customer__in=connected_customers)
            | Q(project__in=connected_projects)
            | Q(permissions__user=user, permissions__is_active=True),
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
            if not settings.WALDUR_MARKETPLACE['ANONYMOUS_USER_CAN_VIEW_OFFERINGS']:
                return self.none()
            else:
                return queryset.filter(shared=True)

        if user.is_staff or user.is_support:
            plans = models.Plan.objects.filter(archived=False)
            return queryset.filter(
                Q(shared=True) | Q(plans__in=plans) | Q(parent__plans__in=plans)
            ).distinct()

        # filtering by available plans
        divisions = user.divisions
        plans = models.Plan.objects.filter(
            Q(divisions__isnull=True) | Q(divisions__in=divisions)
        ).filter(archived=False)

        # filtering by customers and projects
        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)

        return queryset.filter(
            Q(shared=True)
            | (
                (
                    Q(customer__in=connected_customers)
                    | Q(project__in=connected_projects)
                    | Q(permissions__user=user, permissions__is_active=True)
                )
                & (Q(plans__in=plans) | Q(parent__plans__in=plans))
            )
        ).distinct()

    def filter_for_customer(self, value):
        customer = structure_models.Customer.objects.get(uuid=value)
        return self.filter(
            Q(shared=True, divisions__isnull=True)
            | Q(shared=True, divisions__isnull=False, divisions=customer.division)
            | Q(customer__uuid=value)
        )

    def filter_for_service_manager(self, value):
        return self.filter(
            shared=True, permissions__user__uuid=value, permissions__is_active=True
        )

    def filter_for_project(self, value):
        return self.filter(Q(shared=True) | Q(project__uuid=value))

    def filter_importable(self, user):
        # Import is limited to staff for shared offerings and to staff/owners for private offerings

        if user.is_staff:
            return self

        return self.filter(
            shared=False, customer__in=structure_utils.get_customers_owned_by_user(user)
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
            Q(divisions__isnull=True)
            | Q(divisions__isnull=False, divisions=customer.division)
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
            if not settings.WALDUR_MARKETPLACE['ANONYMOUS_USER_CAN_VIEW_PLANS']:
                return self.none()
            else:
                return queryset.filter(offering__shared=True)

        divisions = user.divisions

        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)

        q1 = Q(divisions__isnull=True) | Q(divisions__in=divisions)
        q2 = (
            Q(offering__customer__in=connected_customers)
            | Q(offering__project__in=connected_projects)
            | Q(offering__permissions__user=user, offering__permissions__is_active=True)
        )
        q3 = Q(offering__shared=True)
        return queryset.filter(q3 | (q2 & q1)).distinct()


class PlanManager(MixinManager):
    def get_queryset(self):
        return PlanQuerySet(self.model, using=self._db)
