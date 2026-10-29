import uuid
from unittest import mock
from urllib.parse import urlparse

from ddt import data, ddt
from django.urls import resolve, reverse
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.tests import fixtures


@ddt
class TenantImageUploadTest(test.APITestCase):
    """Uploading an image puts it into the tenant's own OpenStack project, so it
    is for those who manage the tenant's project."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        marketplace_factories.OfferingFactory(scope=self.tenant)
        self.image_id = str(uuid.uuid4())
        for name, value in (
            (
                "create_image",
                {"id": self.image_id, "name": "image", "status": "queued"},
            ),
            ("upload_image_data", {"response": "", "status": 204}),
        ):
            patcher = mock.patch.object(OpenStackBackend, name, return_value=value)
            self.addCleanup(patcher.stop)
            setattr(self, name, patcher.start())

    def create_url(self):
        return reverse(
            "openstack-marketplace-tenant-create-image",
            kwargs={"uuid": self.tenant.uuid.hex},
        )

    def upload_url(self):
        return reverse(
            "openstack-marketplace-tenant-upload-image-data",
            kwargs={"uuid": self.tenant.uuid.hex, "image_id": self.image_id},
        )

    @data("member", "customer_support", "global_support")
    def test_who_only_sees_the_tenant_cannot_upload(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        created = self.client.post(
            self.create_url(), {"name": "image", "visibility": "private"}
        )
        uploaded = self.client.post(
            self.upload_url(), b"image-bytes", content_type="application/octet-stream"
        )

        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(uploaded.status_code, status.HTTP_403_FORBIDDEN)
        self.create_image.assert_not_called()
        self.upload_image_data.assert_not_called()

    @data("staff", "owner", "admin", "manager")
    def test_who_manages_its_project_can_upload(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        created = self.client.post(
            self.create_url(), {"name": "image", "visibility": "private"}
        )
        uploaded = self.client.post(
            self.upload_url(), b"image-bytes", content_type="application/octet-stream"
        )

        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data["image_id"], self.image_id)
        self.assertEqual(
            resolve(urlparse(created.data["upload_url"]).path).url_name,
            "openstack-marketplace-tenant-upload-image-data",
        )
        self.assertEqual(uploaded.status_code, status.HTTP_200_OK, uploaded.data)
        self.upload_image_data.assert_called_once()
