from django.db import models as django_models
from django.db.models import Q

from waldur_core.core import managers as core_managers
from waldur_core.structure import models as structure_models


class MixinManager(core_managers.GenericKeyMixin, django_models.Manager):
    pass


class OfferingQuerySet(django_models.QuerySet):
    def filter_for_user(self, user):
        if user.is_anonymous or user.is_staff or user.is_support:
            return self

        connected_customers = structure_models.Customer.objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )

        connected_projects = structure_models.Project.available_objects.all().filter(
            permissions__user=user, permissions__is_active=True
        )

        return self.filter(
            Q(shared=True)
            | Q(shared=False, customer__in=connected_customers)
            | Q(shared=False, project__in=connected_projects)
            | Q(shared=True, permissions__user=user, permissions__is_active=True),
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

        owned_customers = set(
            structure_models.Customer.objects.all()
            .filter(
                permissions__user=user,
                permissions__is_active=True,
                permissions__role=structure_models.CustomerRole.OWNER,
            )
            .distinct()
        )

        return self.filter(shared=False, customer__in=owned_customers)


class OfferingManager(MixinManager):
    def get_queryset(self):
        return OfferingQuerySet(self.model, using=self._db)
