import datetime
import decimal
from unittest import mock

import requests
from constance.test import override_config
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework.test import APITestCase

from waldur_core.structure import vies
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import handlers, models, tasks, vat
from waldur_mastermind.invoices.tests import factories

D = decimal.Decimal

TEDB_RESPONSE = b"""<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
<env:Body><ns2:retrieveVatRatesRespMsg
    xmlns:ns0="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService:types"
    xmlns:ns2="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService">
<ns0:vatRateResults><ns0:memberState>EE</ns0:memberState><ns0:type>STANDARD</ns0:type>
  <ns0:rate><ns0:type>DEFAULT</ns0:type><ns0:value>24.0</ns0:value></ns0:rate>
  <ns0:situationOn>2026-07-01+02:00</ns0:situationOn></ns0:vatRateResults>
<ns0:vatRateResults><ns0:memberState>EE</ns0:memberState><ns0:type>REDUCED</ns0:type>
  <ns0:rate><ns0:type>REDUCED_RATE</ns0:type><ns0:value>9.0</ns0:value></ns0:rate>
  <ns0:situationOn>2026-07-01+02:00</ns0:situationOn></ns0:vatRateResults>
<ns0:vatRateResults><ns0:memberState>ES</ns0:memberState><ns0:type>STANDARD</ns0:type>
  <ns0:rate><ns0:type>DEFAULT</ns0:type><ns0:value>7.0</ns0:value></ns0:rate>
  <ns0:situationOn>2026-07-01+02:00</ns0:situationOn>
  <ns0:comment>VAT - Canary Islands - </ns0:comment></ns0:vatRateResults>
<ns0:vatRateResults><ns0:memberState>ES</ns0:memberState><ns0:type>STANDARD</ns0:type>
  <ns0:rate><ns0:type>DEFAULT</ns0:type><ns0:value>21.0</ns0:value></ns0:rate>
  <ns0:situationOn>2026-07-01+02:00</ns0:situationOn></ns0:vatRateResults>
<ns0:vatRateResults><ns0:memberState>EL</ns0:memberState><ns0:type>STANDARD</ns0:type>
  <ns0:rate><ns0:type>DEFAULT</ns0:type><ns0:value>24.0</ns0:value></ns0:rate>
  <ns0:situationOn>2025-01-01+01:00</ns0:situationOn></ns0:vatRateResults>
</ns2:retrieveVatRatesRespMsg></env:Body></env:Envelope>"""


def tedb_response(rates):
    results = "".join(
        f"<ns0:vatRateResults><ns0:memberState>{country}</ns0:memberState>"
        "<ns0:type>STANDARD</ns0:type><ns0:rate><ns0:type>DEFAULT</ns0:type>"
        f"<ns0:value>{rate}</ns0:value></ns0:rate>"
        "<ns0:situationOn>2026-07-01+02:00</ns0:situationOn></ns0:vatRateResults>"
        for country, rate in rates.items()
    )
    return (
        '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">'
        '<env:Body><ns2:retrieveVatRatesRespMsg xmlns:ns0="'
        f'{vat.TYPES_NS}" xmlns:ns2="urn:x">{results}'
        "</ns2:retrieveVatRatesRespMsg></env:Body></env:Envelope>"
    ).encode()


def mock_response(content=TEDB_RESPONSE):
    response = mock.Mock(content=content)
    response.raise_for_status.return_value = None
    return response


class ParseRatesTest(APITestCase):
    def test_unexpected_values_are_skipped(self):
        rates = vat.parse_standard_rates(
            tedb_response({"EE": "abc", "DE": "150", "XX": "20", "FI": "25.5"})
        )
        self.assertEqual(rates, {"FI": D("25.5")})

    def test_only_standard_mainland_rates_are_kept(self):
        rates = vat.parse_standard_rates(TEDB_RESPONSE)
        self.assertEqual(
            rates,
            {"EE": D("24.0"), "ES": D("21.0"), "GR": D("24.0")},
        )


@mock.patch("waldur_mastermind.invoices.vat.requests.post")
class RefreshRatesTest(APITestCase):
    @freeze_time("2026-10-14")
    def test_rates_are_stored_for_current_month(self, post):
        post.return_value = mock_response()
        call_command("refresh_vat_rates", stdout=mock.Mock())

        rate = models.VATRate.objects.get(country="EE")
        self.assertEqual((rate.year, rate.month, rate.rate), (2026, 10, D("24")))
        self.assertTrue(models.VATRate.objects.filter(country="GR").exists())
        body = post.call_args[1]["data"].decode()
        self.assertIn("<urn1:isoCode>EL</urn1:isoCode>", body)
        self.assertIn("<urn1:situationOn>2026-10-14</urn1:situationOn>", body)

    @freeze_time("2026-10-01")
    def test_past_month_gets_rates_of_its_last_day(self, post):
        post.return_value = mock_response()
        call_command("refresh_vat_rates", year=2026, month=9, stdout=mock.Mock())

        self.assertIn(
            "<urn1:situationOn>2026-09-30</urn1:situationOn>",
            post.call_args[1]["data"].decode(),
        )
        self.assertEqual(
            models.VATRate.objects.get(country="EE", year=2026, month=9).rate, D("24")
        )

    def test_existing_rates_are_updated(self, post):
        models.VATRate.objects.create(country="EE", rate=22)
        post.return_value = mock_response()
        vat.refresh_vat_rates()
        self.assertEqual(models.VATRate.objects.get(country="EE").rate, D("24"))

    @override_config(VAT_TAXATION_ENABLED=True)
    def test_failed_fetch_keeps_old_rates(self, post):
        models.VATRate.objects.create(country="EE", rate=22)
        post.side_effect = requests.ConnectionError("down")
        tasks.refresh_vat_rates()
        self.assertEqual(models.VATRate.objects.get(country="EE").rate, D("22"))

    @override_config(VAT_TAXATION_ENABLED=False)
    def test_task_is_noop_when_disabled(self, post):
        tasks.refresh_vat_rates()
        post.assert_not_called()

    @freeze_time("2026-10-14")
    def test_ensure_skips_fetch_when_current_month_is_stored(self, post):
        models.VATRate.objects.create(country="EE", rate=24)
        vat.ensure_vat_rates(2026, 10)
        post.assert_not_called()

    def test_ensure_refetches_month_last_fetched_before_it_ended(self, post):
        with freeze_time("2026-09-15"):
            models.VATRate.objects.create(country="EE", rate=22)
        post.return_value = mock_response()
        with freeze_time("2026-10-01"):
            vat.ensure_vat_rates(2026, 9)
        self.assertEqual(
            models.VATRate.objects.get(country="EE", year=2026, month=9).rate, D("24")
        )

    def test_ensure_keeps_month_fetched_after_it_ended(self, post):
        with freeze_time("2026-10-01"):
            models.VATRate.objects.create(country="EE", year=2026, month=9, rate=22)
        with freeze_time("2026-10-02"):
            vat.ensure_vat_rates(2026, 9)
        post.assert_not_called()

    def test_ensure_never_raises(self, post):
        with mock.patch.object(vat, "refresh_vat_rates", side_effect=RuntimeError):
            vat.ensure_vat_rates(2026, 9)

    def test_month_without_rates_falls_back_to_latest_earlier_one(self, post):
        models.VATRate.objects.create(country="EE", year=2026, month=7, rate=22)
        models.VATRate.objects.create(country="EE", year=2026, month=8, rate=24)
        models.VATRate.objects.create(country="EE", year=2026, month=10, rate=25)
        self.assertEqual(vat.get_rates(2026, 9), {"EE": D("24")})
        self.assertEqual(vat.get_rates(2026, 7), {"EE": D("22")})
        self.assertEqual(vat.get_rates(2026, 6), {})

    def test_enabling_vat_queues_refresh(self, post):
        with mock.patch.object(tasks.refresh_vat_rates, "delay") as delay:
            handlers.refresh_vat_rates_when_enabled(
                None, key="VAT_TAXATION_ENABLED", old_value=False, new_value=True
            )
            handlers.refresh_vat_rates_when_enabled(
                None, key="VAT_TAXATION_ENABLED", old_value=True, new_value=False
            )
            handlers.refresh_vat_rates_when_enabled(
                None, key="VAT_OSS_ENABLED", old_value=False, new_value=True
            )
        delay.assert_called_once_with()


EE_OPERATOR = override_settings(
    WALDUR_INVOICES={
        **settings.WALDUR_INVOICES,
        "ISSUER_DETAILS": {"country_code": "EE"},
    }
)


@EE_OPERATOR
class VATRulesTest(APITestCase):
    def setUp(self):
        for country, rate in (("EE", 24), ("DE", 19), ("FI", "25.5")):
            models.VATRate.objects.create(country=country, rate=rate)

    def percent(self, **kwargs):
        customer = structure_factories.CustomerFactory(**kwargs)
        return vat.get_vat_percent(customer)

    def test_customer_outside_eu(self):
        self.assertEqual(self.percent(country="US"), 0)
        self.assertEqual(self.percent(country="NO", is_private_person=True), 0)

    def test_customer_without_country_gets_operator_rate(self):
        self.assertEqual(self.percent(country=""), 24)

    def test_customer_from_operator_country(self):
        self.assertEqual(self.percent(country="EE"), 24)
        self.assertEqual(
            self.percent(
                country="EE", vat_code="EE123456789", vat_verified_code="EE123456789"
            ),
            24,
        )
        self.assertEqual(self.percent(country="EE", is_private_person=True), 24)

    def test_verified_vat_number_is_zero_rated(self):
        self.assertEqual(
            self.percent(
                country="DE", vat_code="DE 123456789", vat_verified_code="DE123456789"
            ),
            0,
        )

    def test_unverified_vat_number_gets_operator_rate(self):
        self.assertEqual(self.percent(country="DE", vat_code="DE123456789"), 24)
        self.assertEqual(
            self.percent(
                country="DE", vat_code="DE999999999", vat_verified_code="DE123456789"
            ),
            24,
        )

    def test_unregistered_eu_legal_person_gets_operator_rate(self):
        self.assertEqual(self.percent(country="DE"), 24)

    @override_config(VAT_OSS_ENABLED=True)
    def test_unregistered_eu_legal_person_is_taxed_as_private_person(self):
        self.assertEqual(self.percent(country="DE"), 19)
        self.assertEqual(
            self.percent(
                country="DE", vat_code="DE999999999", vat_verified_code="DE123456789"
            ),
            19,
        )

    @override_config(VAT_OSS_ENABLED=True)
    def test_eu_private_person_with_oss(self):
        self.assertEqual(self.percent(country="DE", is_private_person=True), 19)
        self.assertEqual(self.percent(country="FI", is_private_person=True), D("25.5"))

    @override_config(VAT_OSS_ENABLED=False)
    def test_eu_private_person_without_oss(self):
        self.assertEqual(self.percent(country="DE", is_private_person=True), 24)

    @override_config(VAT_OSS_ENABLED=True)
    def test_missing_rate_raises(self):
        with self.assertRaises(vat.VATError):
            self.percent(country="FR", is_private_person=True)

    @override_settings(
        WALDUR_INVOICES={"ISSUER_DETAILS": {"country_code": "NO"}},
    )
    def test_non_eu_operator_raises(self):
        with self.assertRaises(vat.VATError):
            self.percent(country="DE")

    @override_settings(
        WALDUR_INVOICES={"ISSUER_DETAILS": {}},
        WALDUR_CORE={**settings.WALDUR_CORE, "SELLER_COUNTRY_CODE": None},
    )
    def test_missing_operator_country_raises(self):
        with self.assertRaises(vat.VATError):
            self.percent(country="DE")

    @override_settings(
        WALDUR_INVOICES={"ISSUER_DETAILS": {}},
        WALDUR_CORE={**settings.WALDUR_CORE, "SELLER_COUNTRY_CODE": "DE"},
    )
    def test_seller_country_code_fallback(self):
        self.assertEqual(self.percent(country="DE"), 19)


@EE_OPERATOR
@mock.patch("waldur_core.structure.vies.check_vat_number")
class RecheckVATCodeTest(APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory(
            country="DE", vat_code="DE123456789", vat_verified_code="DE123456789"
        )

    def test_deregistered_number_loses_verification(self, check):
        check.return_value = vies.VIESResult(valid=False)
        vat.recheck_vat_code(self.customer)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_vat_code_verified)

    def test_unavailable_vies_keeps_last_result(self, check):
        check.side_effect = vies.VIESUnavailable
        vat.recheck_vat_code(self.customer)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_vat_code_verified)

    def test_unverified_number_becomes_verified(self, check):
        self.customer.vat_verified_code = ""
        self.customer.save()
        check.return_value = vies.VIESResult(valid=True)
        vat.recheck_vat_code(self.customer)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_vat_code_verified)
        self.assertIsNotNone(self.customer.vat_verified_at)

    def test_domestic_and_private_customers_are_not_checked(self, check):
        for kwargs in ({"country": "EE"}, {"is_private_person": True}):
            customer = structure_factories.CustomerFactory(
                **{"country": "DE", "vat_code": "DE123456789", **kwargs}
            )
            vat.recheck_vat_code(customer)
        check.assert_not_called()


@EE_OPERATOR
class InvoiceTaxPercentTest(APITestCase):
    def setUp(self):
        # Invoices below are for September 2026.
        models.VATRate.objects.create(country="EE", year=2026, month=9, rate=24)
        models.VATRate.objects.create(country="DE", year=2026, month=9, rate=19)
        self.customer = structure_factories.CustomerFactory(
            country="DE", is_private_person=True, default_tax_percent=10
        )

    def make_invoice(self, **kwargs):
        with freeze_time("2026-09-15"):
            invoice = factories.InvoiceFactory(customer=self.customer, **kwargs)
            factories.InvoiceItemFactory(invoice=invoice, unit_price=100, quantity=1)
        return invoice

    def finalize(self):
        with (
            freeze_time("2026-10-01"),
            mock.patch.object(vat, "ensure_vat_rates"),
        ):
            tasks.create_monthly_invoices()

    @override_config(VAT_TAXATION_ENABLED=False)
    def test_default_tax_percent_used_when_disabled(self):
        invoice = self.make_invoice()
        self.assertEqual(invoice.tax_percent, 10)
        self.finalize()
        invoice.refresh_from_db()
        self.assertEqual(invoice.tax_percent, 10)

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    def test_vat_overrides_default_tax_percent_on_creation(self):
        invoice = self.make_invoice()
        self.assertEqual(invoice.tax_percent, 19)

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    def test_creation_falls_back_to_default_without_rate(self):
        models.VATRate.objects.all().delete()
        invoice = self.make_invoice()
        self.assertEqual(invoice.tax_percent, 10)

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    def test_rate_change_on_the_first_does_not_reach_previous_month(self):
        invoice = self.make_invoice()
        models.VATRate.objects.create(country="DE", year=2026, month=10, rate=21)
        models.VATRate.objects.create(country="EE", year=2026, month=10, rate=24)
        self.finalize()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)
        self.assertEqual(invoice.tax_percent, 19)
        october = models.Invoice.objects.get(
            customer=self.customer, year=2026, month=10
        )
        self.assertEqual(october.tax_percent, 21)

    @override_config(VAT_TAXATION_ENABLED=True)
    @mock.patch("waldur_core.structure.vies.check_vat_number")
    def test_verified_company_is_reverse_charged(self, check):
        check.return_value = vies.VIESResult(valid=True)
        self.customer.is_private_person = False
        self.customer.vat_code = self.customer.vat_verified_code = "DE123456789"
        self.customer.save()
        invoice = self.make_invoice()
        self.finalize()

        invoice.refresh_from_db()
        self.assertEqual(invoice.tax_percent, 0)
        self.assertTrue(invoice.vat_reverse_charge)
        check.assert_called_once()

    @override_config(VAT_TAXATION_ENABLED=True)
    def test_company_outside_eu_is_reverse_charged(self):
        self.customer.is_private_person = False
        self.customer.country = "US"
        self.customer.save()
        invoice = self.make_invoice()
        self.assertEqual(invoice.tax_percent, 0)
        self.assertTrue(invoice.vat_reverse_charge)

    @override_config(VAT_TAXATION_ENABLED=True)
    def test_private_person_outside_eu_is_not_reverse_charged(self):
        self.customer.country = "US"
        self.customer.save()
        invoice = self.make_invoice()
        self.assertEqual(invoice.tax_percent, 0)
        self.assertFalse(invoice.vat_reverse_charge)

    @override_config(VAT_TAXATION_ENABLED=True)
    @mock.patch("waldur_core.structure.vies.check_vat_number")
    def test_deregistered_company_loses_reverse_charge(self, check):
        check.return_value = vies.VIESResult(valid=False)
        self.customer.is_private_person = False
        self.customer.vat_code = self.customer.vat_verified_code = "DE123456789"
        self.customer.save()
        invoice = self.make_invoice()
        self.assertTrue(invoice.vat_reverse_charge)
        self.finalize()

        invoice.refresh_from_db()
        self.assertEqual(invoice.tax_percent, 24)
        self.assertFalse(invoice.vat_reverse_charge)

    def test_vat_is_recalculated_on_finalization(self):
        with override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=False):
            invoice = self.make_invoice()
            self.assertEqual(invoice.tax_percent, 24)

        with override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True):
            self.finalize()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)
        self.assertEqual(invoice.tax_percent, 19)
        self.assertEqual(invoice.total_cost, D("119.00"))

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    def test_invoice_without_rate_is_not_finalized(self):
        invoice = self.make_invoice()
        models.VATRate.objects.filter(country="DE").delete()
        self.finalize()
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING)

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    def test_set_created_keeps_stored_tax_percent(self):
        # Re-finalizing an issued invoice (e.g. rebill_historical_usage) must
        # not rewrite its VAT with today's rates.
        invoice = self.make_invoice()
        models.VATRate.objects.filter(country="DE").update(rate=21)
        invoice.set_created()
        invoice.refresh_from_db()
        self.assertEqual(invoice.tax_percent, 19)

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    def test_refresh_failure_does_not_stop_invoicing(self):
        invoice = self.make_invoice()
        models.VATRate.objects.update(
            fetched_at=timezone.now() - datetime.timedelta(days=40)
        )
        with (
            freeze_time("2026-10-01"),
            mock.patch.object(vat, "refresh_vat_rates", side_effect=RuntimeError),
        ):
            tasks.create_monthly_invoices()
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)
        self.assertEqual(invoice.tax_percent, 19)

    @override_config(VAT_TAXATION_ENABLED=True, VAT_OSS_ENABLED=True)
    @mock.patch("waldur_mastermind.invoices.vat.requests.post")
    def test_monthly_invoicing_refreshes_stale_rates(self, post):
        post.return_value = mock_response(tedb_response({"EE": 24, "DE": 21}))
        invoice = self.make_invoice()
        models.VATRate.objects.update(
            fetched_at=timezone.now() - datetime.timedelta(days=40)
        )
        with freeze_time("2026-10-01"):
            tasks.create_monthly_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)
        self.assertEqual(invoice.tax_percent, 21)
