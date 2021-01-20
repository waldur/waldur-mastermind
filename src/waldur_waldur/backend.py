from django.utils.functional import cached_property

from waldur_core.structure import ServiceBackend, ServiceBackendError
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_waldur.apps import RemoteWaldurConfig

from .client import WaldurClient
from .exceptions import WaldurClientException


class WaldurBackend(ServiceBackend):
    def __init__(self, settings: structure_models.ServiceSettings):
        self.settings = settings

    @cached_property
    def client(self):
        client = WaldurClient(self.settings.backend_url, self.settings.token)
        return client

    def ping(self, raise_exception=False):
        try:
            self.client.ping()
        except WaldurClientException as e:
            if raise_exception:
                raise ServiceBackendError(e)
            return False
        else:
            return True

    def get_shared_offerings(self, customer_uuid=''):
        return self.client.list_public_offerings(customer_uuid)

    def get_importable_offerings(self, customer_uuid=''):
        offerings = self.get_shared_offerings(customer_uuid)
        return [
            offering
            for offering in offerings
            if not marketplace_models.Offering.objects.filter(
                scope__type=RemoteWaldurConfig.service_name,
                customer__uuid=offering['customer_uuid'],
                backend_id=offering['uuid'],
            ).exists()
        ]

    def get_remote_customers(self):
        return self.client.list_remote_customers()
