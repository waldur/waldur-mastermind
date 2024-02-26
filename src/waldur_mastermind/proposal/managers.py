from django.db import models as django_models

from waldur_core.core.models import User
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.marketplace.managers import MixinManager


class CallQuerySet(django_models.QuerySet):
    def filter_for_user(self, user: User):
        if user.is_anonymous:
            return self.none()

        if user.is_staff or user.is_support:
            return self

        return self.filter(customer__in=get_connected_customers(user))


class CallManager(MixinManager):
    def get_queryset(self):
        return CallQuerySet(self.model, using=self._db)
