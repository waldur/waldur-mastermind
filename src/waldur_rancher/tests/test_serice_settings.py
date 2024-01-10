from rest_framework import test

from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack.tests import fixtures as openstack_fixtures
from waldur_rancher import serializers as rancher_serializers


class ServiceSettingsValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()

    def test_validate_management_tenant_uuid(self):
        payload = self.get_payload()
        serializer = rancher_serializers.RancherServiceSerializer(
            data=payload, context=self.get_context(self.fixture.owner)
        )
        self.assertTrue(serializer.is_valid())

        payload.pop("management_tenant_uuid")
        serializer = rancher_serializers.RancherServiceSerializer(
            data=payload, context=self.get_context(self.fixture.owner)
        )
        self.assertTrue(serializer.is_valid())

        tenant = openstack_factories.TenantFactory()
        payload["management_tenant_uuid"] = tenant.uuid.hex
        serializer = rancher_serializers.RancherServiceSerializer(
            data=payload, context=self.get_context(self.fixture.owner)
        )
        self.assertFalse(serializer.is_valid())

    def get_payload(self):
        return {
            "backend_url": "localhost",
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
