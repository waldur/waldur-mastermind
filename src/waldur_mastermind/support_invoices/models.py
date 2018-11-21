from django.db import models

from django.contrib.contenttypes.models import ContentType
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support import models as support_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME


class RequestBasedManager(models.Manager):
    def get_queryset(self):
        offering_model_type = ContentType.objects.get_for_model(support_models.Offering)
        offering_ids = [item.object_id for item in
                        marketplace_models.Resource.objects.filter(content_type=offering_model_type.id)]
        return super(RequestBasedManager, self).get_queryset().filter(pk__in=offering_ids)


class RequestBasedOffering(support_models.Offering):
    objects = RequestBasedManager()

    @staticmethod
    def is_request_based(offering):
        return marketplace_models.Offering.objects.filter(scope=offering.template, type=PLUGIN_NAME).exists()

    class Meta:
        proxy = True
