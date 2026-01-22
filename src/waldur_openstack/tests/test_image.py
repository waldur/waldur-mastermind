from datetime import timedelta
from typing import cast

from django.utils import timezone
from rest_framework import status, test

from . import factories, fixtures


class ImageListDuplicateNamesTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.list_url = factories.ImageFactory.get_list_url()
        self.url = (
            f"{factories.ImageFactory.get_list_url()}"
            f"?tenant_uuid={self.tenant.uuid.hex}"
        )
        self.client.force_authenticate(self.fixture.admin)

        now = timezone.now()
        self.image_old = factories.ImageFactory(
            settings=self.fixture.settings,
            name="Ubuntu",
        )
        self.image_old.tenants.add(self.tenant)
        self.image_old.backend_created_at = now - timedelta(days=1)
        self.image_old.save(update_fields=["backend_created_at"])

        self.image_new = factories.ImageFactory(
            settings=self.fixture.settings,
            name="Ubuntu",
        )
        self.image_new.tenants.add(self.tenant)
        self.image_new.backend_created_at = now
        self.image_new.save(update_fields=["backend_created_at"])

        self.image_unique = factories.ImageFactory(
            settings=self.fixture.settings,
            name="Debian",
        )
        self.image_unique.tenants.add(self.tenant)
        self.image_unique.backend_created_at = now - timedelta(hours=2)
        self.image_unique.save(update_fields=["backend_created_at"])

    def test_list_returns_latest_image_per_name_by_default(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        data = cast(list[dict], response.data)
        uuids = {item["uuid"] for item in data}
        self.assertEqual(len(data), 2)
        self.assertIn(self.image_new.uuid.hex, uuids)
        self.assertIn(self.image_unique.uuid.hex, uuids)
        self.assertNotIn(self.image_old.uuid.hex, uuids)

    def test_show_duplicate_names_returns_all_images(self):
        response = self.client.get(f"{self.url}&show_duplicate_names=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        data = cast(list[dict], response.data)
        uuids = {item["uuid"] for item in data}
        self.assertEqual(len(data), 3)
        self.assertIn(self.image_old.uuid.hex, uuids)
        self.assertIn(self.image_new.uuid.hex, uuids)
        self.assertIn(self.image_unique.uuid.hex, uuids)

    def test_duplicates_are_not_merged_across_settings(self):
        other_settings = factories.SettingsFactory(
            customer=self.fixture.customer,
            shared=True,
        )
        image_other_settings = factories.ImageFactory(
            settings=other_settings,
            name="Ubuntu",
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        data = cast(list[dict], response.data)
        uuids = {item["uuid"] for item in data}
        self.assertIn(self.image_new.uuid.hex, uuids)
        self.assertIn(image_other_settings.uuid.hex, uuids)
