from django.db import models as django_models


class SupportUserQuerySet(django_models.QuerySet):

    def get_or_create_from_user(self, user):
        """ Get or create support user based on regular user """
        return self.get_or_create(user=user, defaults={'name': user.full_name or user.username})


SupportUserManager = django_models.Manager.from_queryset(SupportUserQuerySet)
