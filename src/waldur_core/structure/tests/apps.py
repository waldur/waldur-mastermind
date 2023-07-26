from django.apps import AppConfig

from waldur_core.structure.backend import ServiceBackend
from waldur_core.structure.registry import SupportedServices


class TestBackend(ServiceBackend):
    __test__ = False

    def destroy(self, resource, force=False):
        pass

    def ping(self, *args, **kwargs):
        return


class TestConfig(AppConfig):
    __test__ = False

    name = 'waldur_core.structure.tests'
    label = 'structure_tests'
    service_name = 'Test'

    def ready(self):
        SupportedServices.register_backend(TestBackend)
