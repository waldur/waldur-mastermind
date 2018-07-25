from django.test import TestCase

from waldur_core.structure import SupportedServices, ServiceBackendNotImplemented
from waldur_core.structure.tests import TestConfig, TestBackend
from waldur_core.structure.tests.models import TestService, TestNewInstance
from waldur_core.structure.tests.serializers import ServiceSerializer


class ServiceRegistryTest(TestCase):
    def test_invalid_service_type(self):
        self.assertRaises(ServiceBackendNotImplemented,
                          SupportedServices.get_service_backend, 'invalid')

    def test_get_service_backend(self):
        self.assertEqual(TestBackend,
                         SupportedServices.get_service_backend(TestConfig.service_name))

    def test_get_service_serializer(self):
        self.assertEqual(ServiceSerializer,
                         SupportedServices.get_service_serializer(TestService))

    def test_get_service_resources(self):
        self.assertEqual([TestNewInstance],
                         SupportedServices.get_service_resources(TestService))

    def test_model_key(self):
        self.assertEqual(TestConfig.service_name,
                         SupportedServices.get_model_key(TestNewInstance))
