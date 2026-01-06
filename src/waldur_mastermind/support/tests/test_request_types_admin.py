from rest_framework import status

from waldur_mastermind.support import models
from waldur_mastermind.support.tests import base, factories


class RequestTypeAdminListTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.list_url = factories.RequestTypeFactory.get_list_url()
        # Create both active and inactive request types
        self.active_type = factories.RequestTypeFactory(
            name="Active Type", is_active=True, backend_id=123
        )
        self.inactive_type = factories.RequestTypeFactory(
            name="Inactive Type", is_active=False, backend_id=None
        )

    def test_staff_can_list_all_request_types(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Active Type", names)
        self.assertIn("Inactive Type", names)

    def test_support_cannot_list_request_types(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_list_request_types(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_is_synced_field_reflects_backend_id_presence(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        synced_type = next(
            item for item in response.data if item["name"] == "Active Type"
        )
        manual_type = next(
            item for item in response.data if item["name"] == "Inactive Type"
        )

        self.assertTrue(synced_type["is_synced"])
        self.assertFalse(manual_type["is_synced"])

    def test_filter_by_is_active(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.list_url, {"is_active": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Active Type", names)
        self.assertNotIn("Inactive Type", names)

    def test_filter_by_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.list_url, {"name": "Inactive"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Inactive Type", names)
        self.assertNotIn("Active Type", names)


class RequestTypeAdminCreateTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.list_url = factories.RequestTypeFactory.get_list_url()

    def test_staff_can_create_request_type(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "name": "New Request Type",
            "issue_type_name": "Task",
            "order": 10,
            "is_active": True,
        }
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.RequestType.objects.filter(name="New Request Type").exists()
        )

    def test_support_cannot_create_request_type(self):
        self.client.force_authenticate(self.fixture.global_support)
        data = {"name": "New Request Type", "issue_type_name": "Task"}
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_create_request_type(self):
        self.client.force_authenticate(self.fixture.user)
        data = {"name": "New Request Type", "issue_type_name": "Task"}
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RequestTypeAdminUpdateTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.request_type = factories.RequestTypeFactory(name="Original Name")
        self.url = factories.RequestTypeFactory.get_url(self.request_type)

    def test_staff_can_update_request_type(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "name": "Updated Name",
            "issue_type_name": self.request_type.issue_type_name,
        }
        response = self.client.patch(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.request_type.refresh_from_db()
        self.assertEqual(self.request_type.name, "Updated Name")

    def test_support_cannot_update_request_type(self):
        self.client.force_authenticate(self.fixture.global_support)
        data = {"name": "Updated Name"}
        response = self.client.patch(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RequestTypeAdminDeleteTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.request_type = factories.RequestTypeFactory(name="To Be Deleted")
        self.url = factories.RequestTypeFactory.get_url(self.request_type)

    def test_staff_can_delete_request_type(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.RequestType.objects.filter(uuid=self.request_type.uuid).exists()
        )

    def test_support_cannot_delete_request_type(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RequestTypeAdminActivateTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.request_type = factories.RequestTypeFactory(
            name="Inactive Type", is_active=False
        )
        self.url = factories.RequestTypeFactory.get_url(
            self.request_type, action="activate"
        )

    def test_staff_can_activate_request_type(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.request_type.refresh_from_db()
        self.assertTrue(self.request_type.is_active)

    def test_support_cannot_activate_request_type(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RequestTypeAdminDeactivateTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.request_type = factories.RequestTypeFactory(
            name="Active Type", is_active=True
        )
        self.url = factories.RequestTypeFactory.get_url(
            self.request_type, action="deactivate"
        )

    def test_staff_can_deactivate_request_type(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.request_type.refresh_from_db()
        self.assertFalse(self.request_type.is_active)

    def test_support_cannot_deactivate_request_type(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RequestTypeAdminReorderTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.type1 = factories.RequestTypeFactory(name="Type 1", order=1)
        self.type2 = factories.RequestTypeFactory(name="Type 2", order=2)
        self.type3 = factories.RequestTypeFactory(name="Type 3", order=3)
        self.url = factories.RequestTypeFactory.get_list_url() + "reorder/"

    def test_staff_can_reorder_request_types(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "items": [
                {"uuid": str(self.type1.uuid), "order": 3},
                {"uuid": str(self.type2.uuid), "order": 1},
                {"uuid": str(self.type3.uuid), "order": 2},
            ]
        }
        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.type1.refresh_from_db()
        self.type2.refresh_from_db()
        self.type3.refresh_from_db()

        self.assertEqual(self.type1.order, 3)
        self.assertEqual(self.type2.order, 1)
        self.assertEqual(self.type3.order, 2)

    def test_support_cannot_reorder_request_types(self):
        self.client.force_authenticate(self.fixture.global_support)
        data = {"items": [{"uuid": str(self.type1.uuid), "order": 10}]}
        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RequestTypeAdminSerializerTest(base.BaseTest):
    """Test that serializer returns all expected fields and URL resolution works."""

    def setUp(self):
        super().setUp()
        self.request_type = factories.RequestTypeFactory(
            name="Test Type",
            backend_id=456,
            backend_name="Service Request",
            is_active=True,
            order=5,
        )
        self.url = factories.RequestTypeFactory.get_url(self.request_type)

    def test_serializer_returns_all_fields(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check all expected fields are present
        expected_fields = [
            "url",
            "uuid",
            "name",
            "issue_type_name",
            "backend_id",
            "backend_name",
            "is_active",
            "order",
            "is_synced",
        ]
        for field in expected_fields:
            self.assertIn(field, response.data)

    def test_url_field_is_valid(self):
        """Ensure URL field resolves correctly - catches URL configuration errors."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # URL should contain the correct API path
        self.assertIn("/api/support-request-types-admin/", response.data["url"])
        # URL should contain the uuid
        self.assertIn(str(self.request_type.uuid.hex), response.data["url"])
