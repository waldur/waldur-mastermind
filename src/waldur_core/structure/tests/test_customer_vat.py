from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import vies
from waldur_core.structure.tests import factories, fixtures
from waldur_mastermind.invoices.models import VATRate


def vies_response(data):
    response = mock.Mock()
    response.json.return_value = data
    return response


class CheckVATNumberTest(test.APITestCase):
    @mock.patch("waldur_core.structure.vies.requests.post")
    def test_request_uses_eu_country_code_and_bare_number(self, post):
        post.return_value = vies_response(
            {"valid": True, "name": "Bank", "address": "---"}
        )
        result = vies.check_vat_number("el 094-014-201", "GR")
        self.assertEqual(result, vies.VIESResult(valid=True, name="Bank"))
        self.assertEqual(
            post.call_args[1]["json"], {"countryCode": "EL", "vatNumber": "094014201"}
        )

    @mock.patch("waldur_core.structure.vies.requests.post")
    def test_number_without_prefix_uses_customer_country(self, post):
        post.return_value = vies_response({"valid": False})
        vies.check_vat_number("123456789", "DE")
        self.assertEqual(
            post.call_args[1]["json"], {"countryCode": "DE", "vatNumber": "123456789"}
        )

    @mock.patch("waldur_core.structure.vies.requests.post")
    def test_invalid_input_is_invalid(self, post):
        post.return_value = vies_response(
            {"actionSucceed": False, "errorWrappers": [{"error": "INVALID_INPUT"}]}
        )
        self.assertFalse(vies.check_vat_number("DE1", "DE").valid)

    @mock.patch("waldur_core.structure.vies.requests.post")
    def test_member_state_outage_is_unavailable(self, post):
        post.return_value = vies_response(
            {"actionSucceed": False, "errorWrappers": [{"error": "MS_UNAVAILABLE"}]}
        )
        with self.assertRaises(vies.VIESUnavailable):
            vies.check_vat_number("DE123456789", "DE")

    @mock.patch("waldur_core.structure.vies.requests.post")
    def test_requester_error_is_unavailable(self, post):
        # It concerns requester details, which are never sent.
        post.return_value = vies_response(
            {
                "actionSucceed": False,
                "errorWrappers": [{"error": "INVALID_REQUESTER_INFO"}],
            }
        )
        with self.assertRaises(vies.VIESUnavailable):
            vies.check_vat_number("DE123456789", "DE")

    def test_eu_vat_code_detection(self):
        self.assertTrue(vies.is_eu_vat_code("DE123456789"))
        self.assertTrue(vies.is_eu_vat_code("EL094014201"))
        self.assertTrue(vies.is_eu_vat_code("123456789", "DE"))
        self.assertFalse(vies.is_eu_vat_code("NO123456789MVA"))
        self.assertFalse(vies.is_eu_vat_code(""))


@override_config(VAT_TAXATION_ENABLED=True)
@mock.patch("waldur_core.structure.vies.check_vat_number")
class CustomerVATCodeVerificationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.customer = self.fixture.customer
        self.url = factories.CustomerFactory.get_url(self.customer)

    def patch(self, payload, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        return self.client.patch(self.url, payload)

    def test_registered_number_is_verified(self, check):
        check.return_value = vies.VIESResult(valid=True, name="ACME GmbH")
        response = self.patch({"vat_code": "DE123456789", "country": "DE"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["vat_code_verified"])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.vat_verified_code, "DE123456789")
        self.assertEqual(self.customer.vat_name, "ACME GmbH")

    def test_unregistered_number_is_rejected(self, check):
        check.return_value = vies.VIESResult(valid=False)
        response = self.patch({"vat_code": "DE123456789", "country": "DE"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vat_code", response.data)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.vat_code, "")

    def test_number_is_saved_unverified_when_vies_is_down(self, check):
        check.side_effect = vies.VIESUnavailable
        response = self.patch({"vat_code": "DE123456789", "country": "DE"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["vat_code_verified"])

    def test_changed_number_loses_verification(self, check):
        self.customer.vat_code = self.customer.vat_verified_code = "DE123456789"
        self.customer.country = "DE"
        self.customer.save()
        check.side_effect = vies.VIESUnavailable
        response = self.patch({"vat_code": "DE987654321"})
        self.assertFalse(response.data["vat_code_verified"])

    def test_unchanged_number_is_not_rechecked(self, check):
        self.customer.vat_code = "DE123456789"
        self.customer.country = "DE"
        self.customer.save()
        response = self.patch({"email": "billing@example.com"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        check.assert_not_called()

    def test_non_eu_number_is_not_checked(self, check):
        response = self.patch({"vat_code": "NO123456789MVA", "country": "NO"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        check.assert_not_called()

    def test_owner_cannot_change_private_person_flag(self, check):
        self.patch({"is_private_person": True})
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_private_person)

        self.patch({"is_private_person": True}, user=self.fixture.staff)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_private_person)

    def test_only_staff_can_change_country(self, check):
        self.patch({"country": "DE"})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.country, "")

        self.patch({"country": "DE"}, user=self.fixture.staff)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.country, "DE")

    @override_config(VAT_TAXATION_ENABLED=False)
    def test_owner_can_change_country_when_vat_is_disabled(self, check):
        self.patch({"country": "DE"})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.country, "DE")

    @override_config(VAT_TAXATION_ENABLED=False)
    def test_vies_is_not_used_when_vat_is_disabled(self, check):
        response = self.patch({"vat_code": "DE123456789", "country": "DE"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        check.assert_not_called()


@override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=False)
@override_settings(
    WALDUR_INVOICES={
        **settings.WALDUR_INVOICES,
        "ISSUER_DETAILS": {"country_code": "EE"},
    }
)
class CustomerVATRateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.url = factories.CustomerFactory.get_url(self.customer)
        VATRate.objects.create(country="EE", rate=24)
        VATRate.objects.create(country="DE", rate=19)
        self.client.force_authenticate(self.fixture.staff)

    def get(self):
        return self.client.get(self.url).data

    def test_verified_vat_number_shows_zero_rate_and_rule(self):
        self.customer.country = "DE"
        self.customer.vat_code = self.customer.vat_verified_code = "DE123456789"
        self.customer.save()
        data = self.get()
        self.assertEqual(data["vat_percent"], "0.00")
        self.assertEqual(data["vat_rule"], "Verified EU VAT number (reverse charge)")

    def test_domestic_customer_shows_operator_rate(self):
        self.customer.country = "EE"
        self.customer.save()
        self.assertEqual(self.get()["vat_percent"], "24.00")

    @override_settings(WALDUR_INVOICES={"ISSUER_DETAILS": {"country_code": "NO"}})
    def test_rule_explains_why_rate_is_unavailable(self):
        data = self.get()
        self.assertIsNone(data["vat_percent"])
        self.assertIn("not an EU member state", data["vat_rule"])

    def test_tax_percent_is_read_only_even_for_staff(self):
        self.client.patch(self.url, {"default_tax_percent": "15"})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.default_tax_percent, 0)

    @override_config(VAT_TAXATION_ENABLED=False)
    def test_tax_percent_is_editable_and_rate_hidden_when_vat_is_off(self):
        self.client.patch(self.url, {"default_tax_percent": "15"})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.default_tax_percent, 15)
        data = self.get()
        self.assertIsNone(data["vat_percent"])
        self.assertIsNone(data["vat_rule"])

    def test_customer_list_loads_rates_once(self):
        factories.CustomerFactory.create_batch(3, country="DE")
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(factories.CustomerFactory.get_list_url())
        rate_queries = [
            q for q in ctx.captured_queries if "invoices_vatrate" in q["sql"]
        ]
        self.assertEqual(len(rate_queries), 1)
