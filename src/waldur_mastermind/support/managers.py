from django.db import models as django_models
from django.db.models import Q

from waldur_core.structure import utils as structure_utils
from waldur_mastermind.support import backend


class SupportUserQuerySet(django_models.QuerySet):
    def get_or_create_from_user(self, user):
        """Get or create support user based on regular user"""
        return self.get_or_create(
            user=user,
            backend_name=backend.get_active_backend().backend_name,
            defaults={'name': user.full_name or user.username},
        )


SupportUserManager = django_models.Manager.from_queryset(SupportUserQuerySet)


class AttachmentQuerySet(django_models.QuerySet):
    def filter_for_user(self, user):
        if not user.is_staff:
            user_customers = structure_utils.get_customers_owned_by_user(user)
            subquery = Q(issue__customer__in=user_customers) | Q(issue__caller=user)
            return self.filter(subquery)
        return self


AttachmentManager = django_models.Manager.from_queryset(AttachmentQuerySet)
