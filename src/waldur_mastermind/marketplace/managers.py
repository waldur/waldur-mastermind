from django.conf import settings
from django.db import models as django_models
from django.db.models import Q

from waldur_core.core import managers as core_managers
from waldur_core.structure import models as structure_models
from waldur_core.structure import utils as structure_utils

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

        connected_customers = structure_models.Customer.objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )

        connected_projects = structure_models.Project.available_objects.all().filter(
            permissions__user=user, permissions__is_active=True
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
        connected_customers = structure_models.Customer.objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )
        connected_projects = structure_models.Project.available_objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )

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

        return self.filter(
            Q(
                project__permissions__user=user,
                project__permissions__is_active=True,
            )
            | Q(
                project__customer__permissions__user=user,
                project__customer__permissions__is_active=True,
            )
            | Q(
                offering__customer__permissions__user=user,
                offering__customer__permissions__is_active=True,
            )
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


class PlanManager(MixinManager):
    def get_queryset(self):
        return PlanQuerySet(self.model, using=self._db)
