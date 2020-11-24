from django.db import models
from django.utils.translation import ugettext_lazy as _

from waldur_mastermind.marketplace import models as marketplace_models


class GoogleCredentials(models.Model):
    service_provider = models.OneToOneField(
        marketplace_models.ServiceProvider, on_delete=models.CASCADE
    )
    client_id = models.CharField(max_length=255)
    project_id = models.CharField(max_length=255)
    client_secret = models.CharField(max_length=255)
    calendar_token = models.CharField(max_length=255, blank=True)
    calendar_refresh_token = models.CharField(max_length=255, blank=True)

    class Permissions:
        customer_path = 'service_provider__customer'

    class Meta:
        verbose_name = _('Google credentials')
        verbose_name_plural = _('Google credentials')
