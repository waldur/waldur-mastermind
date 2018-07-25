from __future__ import unicode_literals

from django.contrib.contenttypes.models import ContentType
from django.core import serializers as django_serializers
from django.core.management.base import BaseCommand
from reversion.models import Version, Revision

from waldur_core.quotas import models
from waldur_core.quotas.utils import get_models_with_quotas


class Command(BaseCommand):
    """ Recalculate all quotas """

    def handle(self, *args, **options):
        for model in get_models_with_quotas():
            if hasattr(model, 'GLOBAL_COUNT_QUOTA_NAME'):
                quota, _ = models.Quota.objects.get_or_create(name=model.GLOBAL_COUNT_QUOTA_NAME)
                for index, instance in enumerate(model.objects.all().order_by('created')):
                    revision = Revision.objects.create()
                    revision.date_created = instance.created
                    revision.save()

                    quota.usage = index + 1
                    serializer = django_serializers.get_serializer('json')()
                    serialized_data = serializer.serialize([quota])

                    Version.objects.create(
                        revision=revision,
                        object_id=quota.id,
                        object_id_int=quota.id,
                        content_type=ContentType.objects.get_for_model(quota),
                        format='json',
                        serialized_data=serialized_data,
                        object_repr=str(quota),
                    )
