from django.test import TestCase

from waldur_core.structure.exceptions import ServiceBackendNotImplemented
from waldur_core.structure.registry import SupportedServices, get_service_type
from waldur_core.structure.tests import TestBackend, TestConfig
from waldur_core.structure.tests.models import TestNewInstance


class ServiceRegistryTest(TestCase):
    def test_invalid_service_type(self):
        self.assertRaises(
            ServiceBackendNotImplemented,
            SupportedServices.get_service_backend,
            'invalid',
        )

    def test_get_service_backend(self):
        self.assertEqual(
            TestBackend, SupportedServices.get_service_backend(TestConfig.service_name)
        )

    def test_model_key(self):
        self.assertEqual(TestConfig.service_name, get_service_type(TestNewInstance))
