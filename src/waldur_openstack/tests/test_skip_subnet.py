from unittest import mock

from django.http import QueryDict
from rest_framework import test

from waldur_openstack import models, serializers
from waldur_openstack.tests import fixtures


class TenantSerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.settings = self.fixture.settings
        self.project = self.fixture.project

    def test_create_tenant_skips_default_subnet_creation(self):
        validated_data = {
            "name": "test-tenant",
            "service_settings": self.settings,
            "project": self.project,
            "skip_creation_of_default_subnet": True,
            "subnet_cidr": "192.168.42.0/24",
            "security_groups": [],
        }

        # We need to ensure 'user_username' key exists or is handled. The serializer handles defaults in create?
        # No, defaults are handled during validation. We need to manually set defaults if skipping validation.
        validated_data["user_username"] = "test-user"
        validated_data["user_password"] = "password"

        request = mock.Mock()
        request.user = self.fixture.owner
        request.query_params = QueryDict()

        serializer = serializers.OpenStackTenantSerializer(context={"request": request})
        tenant = serializer.create(validated_data)

        self.assertEqual(models.Network.objects.filter(tenant=tenant).count(), 0)
        self.assertEqual(models.SubNet.objects.filter(tenant=tenant).count(), 0)

    def test_create_tenant_creates_default_subnet_by_default(self):
        validated_data = {
            "name": "test-tenant-default",
            "service_settings": self.settings,
            "project": self.project,
            "subnet_cidr": "192.168.42.0/24",
            "security_groups": [],
        }
        validated_data["user_username"] = "test-user-default"
        validated_data["user_password"] = "password"

        request = mock.Mock()
        request.user = self.fixture.owner
        request.query_params = QueryDict()

        serializer = serializers.OpenStackTenantSerializer(context={"request": request})
        tenant = serializer.create(validated_data)

        self.assertEqual(models.Network.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(models.SubNet.objects.filter(tenant=tenant).count(), 1)
