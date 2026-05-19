from django import forms
from django.test import TestCase
from rest_framework import status, test

from waldur_core.core.constance_admin import WaldurConstanceForm
from waldur_core.structure.tests.factories import UserFactory


class ConstanceAdminFormChoicesTest(TestCase):
    """Verify admin form injects CONSTANCE_CONFIG_CHOICES into dropdown fields."""

    def setUp(self):
        self.form = WaldurConstanceForm(initial={})

    def test_choice_field_has_configured_choices(self):
        field = self.form.fields["LOGIN_PAGE_LAYOUT"]
        self.assertIsInstance(field, forms.ChoiceField)
        self.assertNotIsInstance(field, forms.MultipleChoiceField)
        values = [value for value, _ in field.choices]
        self.assertIn("split-screen", values)
        self.assertIn("centered-card", values)

    def test_choice_field_validates_against_choices(self):
        field = self.form.fields["LOGIN_PAGE_LAYOUT"]
        self.assertEqual(field.clean("split-screen"), "split-screen")
        with self.assertRaises(forms.ValidationError):
            field.clean("not-a-valid-layout")

    def test_multiple_choice_field_becomes_multi_select(self):
        field = self.form.fields["DISABLED_OFFERING_TYPES"]
        self.assertIsInstance(field, forms.MultipleChoiceField)
        values = [value for value, _ in field.choices]
        self.assertIn("OpenStack.Tenant", values)
        self.assertIn("Marketplace.Booking", values)

    def test_multiple_choice_field_validates_against_choices(self):
        field = self.form.fields["DISABLED_OFFERING_TYPES"]
        cleaned = field.clean(["OpenStack.Tenant", "Marketplace.Booking"])
        self.assertEqual(set(cleaned), {"OpenStack.Tenant", "Marketplace.Booking"})
        with self.assertRaises(forms.ValidationError):
            field.clean(["InvalidType"])

    def test_multiple_choice_field_accepts_empty(self):
        field = self.form.fields["DISABLED_OFFERING_TYPES"]
        self.assertEqual(field.clean([]), [])


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
