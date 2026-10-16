from datetime import timedelta

from ddt import data, ddt
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.core import views as core_views
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.invoices.tests import factories, fixtures


class CreateMonthlyInvoiceTest(TestCase):
    def test_invoice_item_is_created_for_created_resource_in_new_month(self):
        with freeze_time("2017-01-15"):
            fixture = fixtures.InvoiceFixture()
            fixture.resource.set_state_ok()
            fixture.resource.save()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        self.assertEqual(models.InvoiceItem.objects.count(), 2)

    def test_invoice_item_is_not_created_for_pending_resource_in_new_month(self):
        with freeze_time("2017-01-15"):
            fixture = fixtures.InvoiceFixture()
            fixture.invoice_item

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        self.assertEqual(models.InvoiceItem.objects.count(), 1)

    def test_old_invoices_are_marked_as_created(self):
        # previous year
        with freeze_time("2016-11-01"):
            invoice1 = factories.InvoiceFactory()

        # previous month
        with freeze_time("2017-01-15"):
            invoice2 = factories.InvoiceFactory()

        with freeze_time("2017-02-4"):
            tasks.create_monthly_invoices()
            invoice1.refresh_from_db()
            self.assertEqual(
                invoice1.state,
                models.Invoice.States.CREATED,
                "Invoice for previous year is not marked as CREATED",
            )

            invoice2.refresh_from_db()
            self.assertEqual(
                invoice2.state,
                models.Invoice.States.CREATED,
                "Invoice for previous month is not marked as CREATED",
            )


@ddt
class CheckAccountingStartDateTest(TestCase):
    @data(
        (True, True, True),  # invoice is created if trial period ended
        (True, False, False),  # invoice is not created if trial period has not ended
        (False, True, True),  # invoice is created if trial period is not enabled
    )
    def test_invoice_created_if_trial_period_disabled_or_ended(self, args):
        skip_trial, accounting_started, invoice_exists = args
        if accounting_started:
            accounting_start_date = timezone.now() - timedelta(days=30)
        else:
            accounting_start_date = timezone.now() + timedelta(days=30)

        with override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=skip_trial):
            customer = structure_factories.CustomerFactory()
            customer.accounting_start_date = accounting_start_date
            customer.save()
            tasks.create_monthly_invoices()
            self.assertEqual(
                invoice_exists,
                models.Invoice.objects.filter(customer=customer).exists(),
            )


@override_settings(task_always_eager=True)
class NotificationTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.fixture.owner
        self.invoice = factories.InvoiceFactory(customer=self.fixture.customer)

    def test_send_invoice(self):
        structure_factories.NotificationFactory(key="invoices.notification")
        tasks.send_invoice_notification(self.invoice.uuid)
        self.assertEqual(len(mail.outbox), 1)
        [(filename, content, mimetype)] = mail.outbox[0].attachments
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF-"))

    def test_invoice_emails_replace_owners(self):
        structure_factories.NotificationFactory(key="invoices.notification")
        self.fixture.customer.invoice_emails = (
            "billing@example.com, finance@example.com,"
        )
        self.fixture.customer.save()

        tasks.send_invoice_notification(self.invoice.uuid)

        recipients = sorted(address for m in mail.outbox for address in m.to)
        self.assertEqual(recipients, ["billing@example.com", "finance@example.com"])

    def test_repeated_invoice_emails_get_one_copy(self):
        structure_factories.NotificationFactory(key="invoices.notification")
        self.fixture.customer.invoice_emails = "billing@example.com,Billing@Example.com"
        self.fixture.customer.save()

        tasks.send_invoice_notification(self.invoice.uuid)

        self.assertEqual(
            [address for m in mail.outbox for address in m.to],
            ["billing@example.com"],
        )

    def test_owners_receive_invoice_when_invoice_emails_empty(self):
        structure_factories.NotificationFactory(key="invoices.notification")

        tasks.send_invoice_notification(self.invoice.uuid)

        self.assertEqual(
            [address for m in mail.outbox for address in m.to],
            [self.fixture.owner.email],
        )


@override_settings(task_always_eager=True)
class InvoiceEmailsChangedNotificationTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.owner = self.fixture.owner
        self.customer = self.fixture.customer
        structure_factories.NotificationFactory(key="invoices.invoice_emails_changed")
        self.url = reverse("customer-contact", kwargs={"uuid": self.customer.uuid.hex})

    def update_invoice_emails(self, value):
        self.client.force_authenticate(self.fixture.staff)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {"invoice_emails": value})
        self.assertEqual(response.status_code, 200, response.data)

    def test_owners_are_told_when_invoice_emails_change(self):
        self.update_invoice_emails("billing@example.com")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn("billing@example.com", message.body)
        self.assertIn(self.fixture.staff.full_name, message.body)

    def test_owners_are_told_when_invoice_emails_are_cleared(self):
        structure_models.Customer.objects.filter(pk=self.customer.pk).update(
            invoice_emails="billing@example.com"
        )

        self.update_invoice_emails("")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("organization owners", mail.outbox[0].body)

    def test_no_mail_when_list_is_only_reformatted(self):
        structure_models.Customer.objects.filter(pk=self.customer.pk).update(
            invoice_emails="billing@example.com"
        )

        self.update_invoice_emails(" billing@example.com, ")

        self.assertEqual(len(mail.outbox), 0)

    def test_no_mail_when_other_contact_fields_change(self):
        self.client.force_authenticate(self.fixture.staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"phone_number": "+372000000"})

        self.assertEqual(len(mail.outbox), 0)


class InvoicesPublicSettingsTest(TestCase):
    def setUp(self):
        cache.delete("API_CONFIGURATION")

    def tearDown(self):
        cache.delete("API_CONFIGURATION")

    def test_send_customer_invoices_is_public(self):
        invoices_settings = {**settings.WALDUR_INVOICES, "SEND_CUSTOMER_INVOICES": True}
        with override_settings(WALDUR_INVOICES=invoices_settings):
            public_settings = core_views.get_public_settings()

        self.assertEqual(
            public_settings["WALDUR_INVOICES"], {"SEND_CUSTOMER_INVOICES": True}
        )


@override_settings(TIME_ZONE="Europe/Tallinn")
class TimezoneConsistencyTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer

    def test_create_monthly_invoices_uses_local_timezone_for_filtering(self):
        """Test that invoice filtering uses local timezone consistently."""
        invoice = factories.InvoiceFactory(
            year=2023, month=8, state=models.Invoice.States.PENDING
        )

        with freeze_time("2023-09-01 06:00:00"):
            tasks.create_monthly_invoices()
            invoice.refresh_from_db()
            self.assertEqual(invoice.state, models.Invoice.States.CREATED)

    def test_timezone_edge_case_at_month_boundary(self):
        """Test the specific timezone bug: UTC+3 task vs UTC filtering."""
        august_invoice = factories.InvoiceFactory(
            year=2023, month=8, state=models.Invoice.States.PENDING
        )
        # UTC time 22:00:00 is 01:00:00 local time for europe/tallinn timezone
        with freeze_time("2023-08-31 22:00:00"):
            tasks.create_monthly_invoices()

            august_invoice.refresh_from_db()
            self.assertEqual(august_invoice.state, models.Invoice.States.CREATED)

    def test_cross_year_invoice_processing(self):
        """Test processing invoices from previous year."""
        december_invoice = factories.InvoiceFactory(
            year=2022, month=12, state=models.Invoice.States.PENDING
        )

        # UTC time 22:00:00 is 01:00:00 local time for europe/tallinn timezone
        with freeze_time("2022-12-31 22:00:00"):
            tasks.create_monthly_invoices()

            december_invoice.refresh_from_db()
            self.assertEqual(december_invoice.state, models.Invoice.States.CREATED)

    def test_multiple_old_invoices_processed(self):
        """Test that multiple old invoices are processed correctly."""
        july_invoice = factories.InvoiceFactory(
            year=2023, month=7, state=models.Invoice.States.PENDING
        )
        august_invoice = factories.InvoiceFactory(
            year=2023, month=8, state=models.Invoice.States.PENDING
        )

        with freeze_time("2023-09-01 06:00:00"):
            tasks.create_monthly_invoices()

            july_invoice.refresh_from_db()
            august_invoice.refresh_from_db()
            self.assertEqual(july_invoice.state, models.Invoice.States.CREATED)
            self.assertEqual(august_invoice.state, models.Invoice.States.CREATED)
            # Check that september invoices were created for customers
            september_invoices = models.Invoice.objects.filter(
                year=2023, month=9, state=models.Invoice.States.PENDING
            )
            self.assertTrue(
                september_invoices.exists(), "September invoices should be created"
            )
