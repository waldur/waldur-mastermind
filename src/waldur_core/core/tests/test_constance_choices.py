from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory


class ConstanceChoicesTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        self.client.force_login(self.staff)

    def test_single_choice_field_validation(self):
        payload = {"DEFAULT_IDP": "eduteams"}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertEqual(response.data["DEFAULT_IDP"], "eduteams")

    def test_single_choice_field_invalid_value(self):
        payload = {"DEFAULT_IDP": "invalid-idp"}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("DEFAULT_IDP", response.data)

    def test_multiple_choice_field_validation(self):
        payload = {
            "DISABLED_OFFERING_TYPES": ["OpenStack.Tenant", "Marketplace.Booking"]
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertEqual(
            set(response.data["DISABLED_OFFERING_TYPES"]),
            {"OpenStack.Tenant", "Marketplace.Booking"},
        )

    def test_multiple_choice_field_invalid_value(self):
        payload = {"DISABLED_OFFERING_TYPES": ["InvalidType"]}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("DISABLED_OFFERING_TYPES", response.data)

    def test_multiple_choice_field_empty_value(self):
        payload = {"DISABLED_OFFERING_TYPES": []}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertEqual(response.data["DISABLED_OFFERING_TYPES"], [])
