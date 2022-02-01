from urllib.parse import urlencode

from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.core import models as core_models
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


class GoogleCalendar(core_models.StateMixin):
    offering = models.OneToOneField(
        marketplace_models.Offering, on_delete=models.CASCADE
    )
    backend_id = models.CharField(max_length=255, db_index=True, null=True, blank=True)
    public = models.BooleanField(default=False)

    @property
    def http_link(self):
        if self.public:
            return 'https://calendar.google.com/calendar/embed?%s' % urlencode(
                {'src': self.backend_id}
            )

    class Permissions:
        customer_path = 'offering__customer'

    class Meta:
        verbose_name = _('Google calendar')
        verbose_name_plural = _('Google calendars')

    def __str__(self):
        return f'{self.offering} ({self.public})'
