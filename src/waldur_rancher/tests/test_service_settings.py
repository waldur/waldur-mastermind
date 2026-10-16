from rest_framework import test

from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_rancher import serializers as rancher_serializers


class ServiceSettingsValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()

    def test_validate_management_tenant_uuid(self):
        payload = self.get_payload()
        serializer = rancher_serializers.RancherServiceSettingsSerializer(
            data=payload, context=self.get_context(self.fixture.owner)
        )
        self.assertTrue(serializer.is_valid())

        payload.pop("management_tenant_uuid")
        serializer = rancher_serializers.RancherServiceSettingsSerializer(
            data=payload, context=self.get_context(self.fixture.owner)
        )
        self.assertTrue(serializer.is_valid())

        tenant = openstack_factories.TenantFactory()
        payload["management_tenant_uuid"] = tenant.uuid.hex
        serializer = rancher_serializers.RancherServiceSettingsSerializer(
            data=payload, context=self.get_context(self.fixture.owner)
        )
        self.assertFalse(serializer.is_valid())

    def get_payload(self):
        return {
            "backend_url": "https://rancher.example.com",
            "username": "user",
            "password": "password",
            "base_image_name": "ubuntu",
            "management_tenant_uuid": self.fixture.tenant.uuid.hex,
        }

    def get_context(self, user):
        class Request:
            def __init__(self, request_user):
                self.user = request_user

        return {"request": Request(user)}


class BackendURLValidationTest(test.APITestCase):
    def _payload(self, backend_url):
        return {
            "backend_url": backend_url,
            "username": "user",
            "password": "password",
            "base_image_name": "ubuntu",
        }

    def test_accepts_supported_backend_urls(self):
        for backend_url in (
            "https://rancher.example.com",
            "http://rancher:8443",
            "http://rancher.example.com:8443",
            "ssh://bastion.example.com",
            "ssh://login01",
            "ldap://directory.example.com",
            "ldaps://directory.example.com",
            "rdp://desktop.example.com",
        ):
            with self.subTest(backend_url=backend_url):
                serializer = rancher_serializers.RancherServiceSettingsSerializer(
                    data=self._payload(backend_url)
                )
                self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_malformed_backend_urls(self):
        for backend_url in (
            "not a url",
            "localhost",
            "example.com",
            "ftp://files.example.com",
            "ftps://files.example.com",
            "http://keystone_1.example.com",
            "http://999.999.999.999",
            "http://256.0.0.1:5000",
            "http://1.2.3",
            "http://1234",
        ):
            with self.subTest(backend_url=backend_url):
                serializer = rancher_serializers.RancherServiceSettingsSerializer(
                    data=self._payload(backend_url)
                )
                self.assertFalse(serializer.is_valid())
                self.assertIn(
                    "Enter a valid URL", str(serializer.errors["backend_url"])
                )
