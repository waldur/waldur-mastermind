from django.apps import AppConfig

from waldur_core.structure import SupportedServices, ServiceBackend

default_app_config = 'waldur_core.structure.tests.TestConfig'


class TestBackend(ServiceBackend):
    def destroy(self, resource, force=False):
        pass


class TestConfig(AppConfig):
    name = 'waldur_core.structure.tests'
    label = 'structure_tests'
    service_name = 'Test'

    def ready(self):
        SupportedServices.register_backend(TestBackend)
        SupportedServices.register_service(self.get_model('TestService'))
