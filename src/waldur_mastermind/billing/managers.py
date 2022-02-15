from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models

from waldur_core.core import managers as core_managers
from waldur_core.structure.managers import filter_queryset_for_user


class UserFilterMixin:
    def filtered_for_user(self, user, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()

        if user.is_staff or user.is_support:
            return queryset

        query = django_models.Q()
        for model in self.get_available_models():
            user_object_ids = filter_queryset_for_user(
                model.objects.all(), user
            ).values_list('id', flat=True)
            content_type_id = ContentType.objects.get_for_model(model).id
            query |= django_models.Q(
                object_id__in=list(user_object_ids), content_type_id=content_type_id
            )

        return queryset.filter(query)

    def get_available_models(self):
        """Return list of models that are acceptable"""
        raise NotImplementedError()


class PriceEstimateManager(core_managers.GenericKeyMixin, django_models.Manager):
    def get_available_models(self):
        return self.model.get_estimated_models()
