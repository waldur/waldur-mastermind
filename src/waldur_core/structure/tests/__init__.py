from django.apps import AppConfig

from waldur_core.structure.backend import ServiceBackend
from waldur_core.structure.registry import SupportedServices

default_app_config = 'waldur_core.structure.tests.TestConfig'


class TestBackend(ServiceBackend):
    __test__ = False

    def destroy(self, resource, force=False):
        pass


class TestConfig(AppConfig):
    __test__ = False

    name = 'waldur_core.structure.tests'
    label = 'structure_tests'
    service_name = 'Test'

    def ready(self):
        SupportedServices.register_backend(TestBackend)
