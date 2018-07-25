from django.contrib.contenttypes import models as ct_models
from django.db import models
from django.db.models import Q

from waldur_core.core.managers import GenericKeyMixin


class QuotaManager(GenericKeyMixin, models.Manager):

    def filtered_for_user(self, user, queryset=None):
        from waldur_core.quotas import utils

        if queryset is None:
            queryset = self.get_queryset()
        # XXX: This circular dependency will be removed then filter_queryset_for_user
        # will be moved to model manager method
        from waldur_core.structure.managers import filter_queryset_for_user

        if user.is_staff or user.is_support:
            return queryset

        quota_scope_models = utils.get_models_with_quotas()
        query = Q()
        for model in quota_scope_models:
            user_object_ids = filter_queryset_for_user(model.objects.all(), user).values_list('id', flat=True)
            content_type_id = ct_models.ContentType.objects.get_for_model(model).id
            query |= Q(object_id__in=user_object_ids, content_type_id=content_type_id)

        return queryset.filter(query)
