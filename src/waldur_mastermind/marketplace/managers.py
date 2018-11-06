from django.db.models import Q
from django.db import models as django_models

from waldur_core.core import managers as core_managers
from waldur_core.structure import models as structure_models


class MixinManager(core_managers.GenericKeyMixin, django_models.Manager):
    pass


class OfferingQuerySet(django_models.QuerySet):
    def filter_for_user(self, user):
        if user.is_staff or user.is_support:
            return self

        connected_customers = set(structure_models.Customer.objects.all().filter(
            Q(permissions__user=user, permissions__is_active=True) |
            Q(projects__permissions__user=user, projects__permissions__is_active=True)
        ).distinct())

        return self.filter(
            Q(shared=True) |
            Q(shared=False, allowed_customers__in=connected_customers) |
            Q(shared=False, customer__in=connected_customers)
        )


class OfferingManager(MixinManager):
    def get_queryset(self):
        return OfferingQuerySet(self.model, using=self._db)
