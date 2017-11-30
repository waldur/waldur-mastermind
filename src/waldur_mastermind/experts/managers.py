from django.db import models
from django.db.models import Q

from waldur_core.structure import models as structure_models


class ExpertRequestQuerySet(models.QuerySet):
    def filtered_for_user(self, user):
        return self.filter(
            project__customer__permissions__is_active=True,
            project__customer__permissions__user=user,
            project__customer__permissions__role=structure_models.CustomerRole.OWNER,
        )

    def filtered_for_manager(self, user):
        user_projects = structure_models.Project.objects.filter(permissions__is_active=True, permissions__user=user).\
            distinct()
        return self.filter(~Q(state=self.model.States.COMPLETED) |
                           Q(state=self.model.States.COMPLETED,
                             contract__team__in=user_projects.all()))


class ExpertRequestManager(models.Manager):
    def get_queryset(self):
        return ExpertRequestQuerySet(self.model, using=self._db)


class ExpertBidQuerySet(models.QuerySet):
    def filtered_for_user(self, user):
        expert_manager_query = Q(
            team__customer__permissions__is_active=True,
            team__customer__permissions__user=user,
            team__customer__permissions__role=structure_models.CustomerRole.OWNER,
        )
        request_customer_query = Q(
            request__project__customer__permissions__is_active=True,
            request__project__customer__permissions__user=user,
            request__project__customer__permissions__role=structure_models.CustomerRole.OWNER,
        )

        if not user.is_staff:
            return self.filter(expert_manager_query | request_customer_query)
        return self


class ExpertBidManager(models.Manager):
    def get_queryset(self):
        return ExpertBidQuerySet(self.model, using=self._db)
