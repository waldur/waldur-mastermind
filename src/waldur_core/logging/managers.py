from django.contrib.contenttypes import models as ct_models
from django.db import models
from django.db.models import Q


# XXX: This manager are very similar with quotas manager
class AlertManager(models.Manager):

    def filtered_for_user(self, user, queryset=None):
        from waldur_core.logging import utils

        if queryset is None:
            queryset = self.get_queryset()
        # XXX: This circular dependency will be removed then filter_queryset_for_user
        # will be moved to model manager method
        from waldur_core.structure.managers import filter_queryset_for_user

        query = Q()
        for model in utils.get_loggable_models():
            user_object_ids = filter_queryset_for_user(model.objects.all(), user).values_list('id', flat=True)
            content_type_id = ct_models.ContentType.objects.get_for_model(model).id
            query |= Q(object_id__in=user_object_ids, content_type_id=content_type_id)

        return queryset.filter(query)

    def for_objects(self, qs):
        kwargs = dict(
            content_type=ct_models.ContentType.objects.get_for_model(qs.model),
            object_id__in=qs.values_list('id', flat=True),
            closed__isnull=True
        )
        return self.get_queryset().filter(**kwargs)
