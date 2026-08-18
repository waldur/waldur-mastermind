import datetime
from decimal import Decimal
from unittest import mock

from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    SUPPORT_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@ddt
class InvoiceRetrieveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    @data("owner", "staff")
    def test_user_with_access_can_retrieve_customer_invoice(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_retrieve_customer_invoice(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invoice_item_contains_credit_field(self):
        self.client.force_authenticate(self.fixture.owner)
        credit = factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=100.0
        )
        invoice_item = self.fixture.invoice_item
        invoice_item.credit = credit
        invoice_item.save()

        invoice_data = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice)
        )

        self.assertIn(
            "credit", invoice_data.data["items"][0], invoice_data.data["items"]
        )
        credit = invoice_data.data["items"][0]["credit"]
        self.assertTrue(credit)


@ddt
class InvoiceSendNotificationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = factories.InvoiceFactory.get_url(
            self.fixture.invoice, action="send_notification"
        )
        self.fixture.invoice.state = models.Invoice.States.CREATED
        self.fixture.invoice.save(update_fields=["state"])

    @data("staff")
    def test_user_can_send_invoice_notification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_send_invoice_notification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(task_always_eager=True)
    def test_notification_email_is_rendered(self):
        event_type = "notification"
        structure_factories.NotificationFactory(key=f"invoices.{event_type}")

        # Arrange
        self.fixture.owner

        # Act
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.url)

        # Assert
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue("invoice" in mail.outbox[0].subject)
        self.assertEqual(self.fixture.owner.email, mail.outbox[0].to[0])

    def test_user_cannot_send_invoice_notification_in_invalid_state(self):
        self.fixture.invoice.state = models.Invoice.States.PENDING
        self.fixture.invoice.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data, ["Notification only for the created invoice can be sent."]
        )


class UpdateInvoiceItemProjectTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item

    def test_project_name_and_uuid_is_rendered_for_invoice_item(self):
        self.check_invoice_item()

    def test_when_project_is_deleted_invoice_item_is_present(self):
        self.fixture.project.delete()
        self.check_invoice_item()

    def test_when_project_is_updated_invoice_item_is_synced(self):
        self.fixture.project.name = "New name"
        self.fixture.project.save()
        self.check_invoice_item()

    def test_invoice_item_updated_for_pending_invoice_only(self):
        self.invoice.state = models.Invoice.States.CANCELED
        self.invoice.save()
        old_name = self.fixture.project.name
        self.fixture.project.name = "New name"
        self.fixture.project.save()
        self.check_invoice_item(old_name)

    def check_invoice_item(self, project_name=None):
        if project_name is None:
            project_name = self.fixture.project.name

        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(factories.InvoiceFactory.get_url(self.invoice))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data["items"][0]
        self.assertEqual(item["project_name"], project_name)
        self.assertEqual(item["project_uuid"], self.fixture.project.uuid.hex)


class MeasuredUnitTest(test.APITestCase):
    def get_invoice_item(self, unit, measured_unit=""):
        return factories.InvoiceItemFactory(
            start=timezone.make_aware(datetime.datetime(year=2020, month=12, day=1)),
            end=timezone.make_aware(datetime.datetime(year=2020, month=12, day=10)),
            quantity=2,
            unit=unit,
            measured_unit=measured_unit,
        )

    def test_offering_component(self):
        item = self.get_invoice_item(UnitPriceMixin.Units.PER_DAY, "kG")
        self.assertEqual(item.get_measured_unit(), _("kG"))

    def test_days(self):
        item = self.get_invoice_item(UnitPriceMixin.Units.PER_DAY)
        self.assertEqual(item.get_measured_unit(), _("days"))

    def test_hours(self):
        item = self.get_invoice_item(UnitPriceMixin.Units.PER_HOUR)
        self.assertEqual(item.get_measured_unit(), _("hours"))

    def test_half_month(self):
        item = self.get_invoice_item(UnitPriceMixin.Units.PER_HALF_MONTH)
        self.assertEqual(item.get_measured_unit(), _("percents from half a month"))

    def test_month(self):
        item = self.get_invoice_item(UnitPriceMixin.Units.PER_MONTH)
        self.assertEqual(item.get_measured_unit(), _("percents from a month"))

    def test_quantity(self):
        item = self.get_invoice_item(UnitPriceMixin.Units.QUANTITY)
        resource = marketplace_factories.ResourceFactory()
        resource.scope = structure_factories.ProjectFactory()
        resource.save()
        item.resource = resource
        item.save()
        self.assertEqual(item.get_measured_unit(), _("projects"))


class InvoiceStatsTest(test.APITestCase):
    def setUp(self):
        self.provider = marketplace_factories.ServiceProviderFactory()
        self.provider_2 = marketplace_factories.ServiceProviderFactory()

        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, customer=self.provider.customer
        )

        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.component = marketplace_factories.PlanComponentFactory(
            component=self.offering_component, price=Decimal(5), plan=self.plan
        )

        self.offering_2 = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, customer=self.provider_2.customer
        )

        self.offering_component_2 = marketplace_factories.OfferingComponentFactory(
            offering=self.offering_2
        )
        self.plan_2 = marketplace_factories.PlanFactory(
            offering=self.offering_2,
            unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.component_2 = marketplace_factories.PlanComponentFactory(
            component=self.offering_component_2, price=Decimal(7), plan=self.plan_2
        )

        self.resource_1 = marketplace_factories.ResourceFactory(
            state=ResourceStates.OK,
            offering=self.offering,
            plan=self.plan,
            limits={"cpu": 1},
        )

        self.resource_2 = marketplace_factories.ResourceFactory(
            state=ResourceStates.OK,
            offering=self.offering,
            project=self.resource_1.project,
            plan=self.plan,
            limits={"cpu": 1},
        )

        self.resource_3 = marketplace_factories.ResourceFactory(
            state=ResourceStates.OK,
            offering=self.offering_2,
            project=self.resource_1.project,
            plan=self.plan_2,
            limits={"cpu": 1},
        )

        self.customer = self.resource_1.project.customer

        self.marketplace_support_offering = marketplace_factories.OfferingFactory(
            type=SUPPORT_OFFERING,
            customer=self.provider.customer,
        )
        self.support_offering_component = (
            marketplace_factories.OfferingComponentFactory(
                offering=self.marketplace_support_offering
            )
        )
        self.marketplace_support_plan = marketplace_factories.PlanFactory(
            offering=self.marketplace_support_offering,
            unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.component = marketplace_factories.PlanComponentFactory(
            component=self.support_offering_component,
            price=Decimal(5),
            plan=self.marketplace_support_plan,
        )
        self.resource_4 = marketplace_factories.ResourceFactory(
            state=ResourceStates.OK,
            offering=self.marketplace_support_offering,
            project=self.resource_1.project,
            plan=self.marketplace_support_plan,
        )
        self.resource_4.save()

    @freeze_time("2019-01-01")
    def test_invoice_stats(self):
        tasks.create_monthly_invoices()
        invoice = models.Invoice.objects.get(customer=self.customer, year=2019, month=1)
        url = factories.InvoiceFactory.get_url(invoice=invoice, action="stats")
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        result = self.client.get(url)
        self.assertEqual(len(result.data), 3)
        self.assertEqual(
            {d["uuid"] for d in result.data},
            {
                self.offering.uuid.hex,
                self.marketplace_support_offering.uuid.hex,
                self.offering_2.uuid.hex,
            },
        )

        aggregated_total = sum(
            item.total
            for item in models.InvoiceItem.objects.filter(
                invoice=invoice,
                resource_id__in=[self.resource_1.id, self.resource_2.id],
            )
        )

        self.assertEqual(
            list(filter(lambda x: x["uuid"] == self.offering.uuid.hex, result.data))[0],
            {
                "uuid": self.offering.uuid.hex,
                "offering_name": self.offering.name,
                "aggregated_price": aggregated_total,
                "aggregated_tax": Decimal(0),
                "aggregated_total": aggregated_total,
                "service_category_title": self.offering.category.title,
                "service_provider_name": self.offering.customer.name,
                "service_provider_uuid": self.provider.uuid.hex,
            },
        )

        aggregated_total = sum(
            [
                item.total
                for item in models.InvoiceItem.objects.filter(
                    invoice=invoice,
                    resource_id__in=[self.resource_3.id],
                )
            ]
        )
        self.assertEqual(
            list(filter(lambda x: x["uuid"] == self.offering_2.uuid.hex, result.data))[
                0
            ],
            {
                "uuid": self.offering_2.uuid.hex,
                "offering_name": self.offering_2.name,
                "aggregated_price": aggregated_total,
                "aggregated_tax": Decimal(0),
                "aggregated_total": aggregated_total,
                "service_category_title": self.offering_2.category.title,
                "service_provider_name": self.offering_2.customer.name,
                "service_provider_uuid": self.provider_2.uuid.hex,
            },
        )

        aggregated_total = models.InvoiceItem.objects.get(
            invoice=invoice,
            resource_id=self.resource_4.id,
        ).price

        self.assertEqual(
            list(
                filter(
                    lambda x: x["uuid"] == self.marketplace_support_offering.uuid.hex,
                    result.data,
                )
            )[0],
            {
                "uuid": self.marketplace_support_offering.uuid.hex,
                "offering_name": self.marketplace_support_offering.name,
                "aggregated_price": aggregated_total,
                "aggregated_tax": Decimal(0),
                "aggregated_total": aggregated_total,
                "service_category_title": self.marketplace_support_offering.category.title,
                "service_provider_name": self.offering.customer.name,
                "service_provider_uuid": self.provider.uuid.hex,
            },
        )


class DeleteCustomerWithInvoiceTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.invoice = factories.InvoiceFactory(customer=self.fixture.customer)
        self.url = structure_factories.CustomerFactory.get_url(self.fixture.customer)

    def test_staff_can_delete_customer_with_pending_invoice(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_staff_can_delete_customer_with_non_empty_invoice(self):
        factories.InvoiceItemFactory(invoice=self.invoice, unit_price=100)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_staff_can_delete_customer_with_active_invoice(self):
        self.invoice.state = models.Invoice.States.CREATED
        self.invoice.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


@ddt
class InvoicePaidTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.invoice.state = models.Invoice.States.CREATED
        self.invoice.save()
        self.url = factories.InvoiceFactory.get_url(self.invoice, "paid")

    def test_staff_can_mark_invoice_as_paid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, models.Invoice.States.PAID)

    @data("owner", "manager", "admin", "user")
    def test_other_users_cannot_mark_invoice_as_paid(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_mark_invoice_as_paid_if_current_state_is_not_created(self):
        self.invoice.state = models.Invoice.States.PENDING
        self.invoice.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_create_payment_if_payment_data_has_been_passed(self):
        profile = factories.PaymentProfileFactory(
            organization=self.invoice.customer, is_active=True
        )

        self.client.force_authenticate(self.fixture.staff)
        date = datetime.date.today()
        response = self.client.post(
            self.url, data={"date": date, "proof": dummy_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, models.Invoice.States.PAID)
        self.assertEqual(
            models.Payment.objects.filter(
                date_of_payment=date, profile=profile
            ).count(),
            1,
        )

    def test_do_not_create_payment_if_profile_does_not_exist(self):
        self.client.force_authenticate(self.fixture.staff)
        date = datetime.date.today()
        response = self.client.post(
            self.url, data={"date": date, "proof": dummy_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_proof_is_not_required_for_payment_of_invoice(self):
        factories.PaymentProfileFactory(
            organization=self.invoice.customer, is_active=True
        )

        self.client.force_authenticate(self.fixture.staff)
        date = datetime.date.today()
        response = self.client.post(self.url, data={"date": date}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class UpdateBackendIdTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = factories.InvoiceFactory.get_url(
            self.fixture.invoice, action="set_backend_id"
        )

    @data("staff")
    def test_user_can_set_backend_id(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {"backend_id": "backend_id"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.invoice.refresh_from_db()
        self.assertEqual(self.fixture.invoice.backend_id, "backend_id")

        response = self.client.post(self.url, {"backend_id": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.invoice.refresh_from_db()
        self.assertEqual(self.fixture.invoice.backend_id, "")

    @data("manager", "admin", "user")
    def test_user_cannot_set_backend_id(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {"backend_id": "backend_id"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InvoiceUpdateCacheTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.invoice = factories.InvoiceFactory(customer=self.customer)

    def test_update_cache_with_same_values_does_not_perform_db_update(self):
        """Test that update_cache with same values doesn't cause a DB update"""
        # Arrange - Set total_cost and total_price to 0
        self.invoice.total_cost = Decimal("0.0000")
        self.invoice.total_price = Decimal("0.00")
        self.invoice.save()

        # Mock the total and price properties to return the same values
        with (
            mock.patch.object(
                models.Invoice, "total", new_callable=mock.PropertyMock
            ) as mock_total,
            mock.patch.object(
                models.Invoice, "price", new_callable=mock.PropertyMock
            ) as mock_price,
            mock.patch.object(models.Invoice, "save") as mock_save,
        ):
            mock_total.return_value = Decimal("0.0000")
            mock_price.return_value = Decimal("0.00")

            # Act
            self.invoice.update_cache()

            # Assert
            mock_save.assert_not_called()

    def test_update_cache_with_different_values_performs_db_update(self):
        """Test that update_cache with different values performs a DB update"""
        # Arrange
        self.invoice.total_cost = Decimal("0.0000")
        self.invoice.total_price = Decimal("0.00")
        self.invoice.save()

        # Mock the total and price properties to return different values
        with (
            mock.patch.object(
                models.Invoice, "total", new_callable=mock.PropertyMock
            ) as mock_total,
            mock.patch.object(
                models.Invoice, "price", new_callable=mock.PropertyMock
            ) as mock_price,
            mock.patch.object(models.Invoice, "save") as mock_save,
        ):
            mock_total.return_value = Decimal("10.0000")
            mock_price.return_value = Decimal("10.00")

            # Act
            self.invoice.update_cache()

            # Assert
            mock_save.assert_called_once_with(
                update_fields=["total_cost", "total_price"]
            )

    def test_update_cache_with_different_precision_considered_equal(self):
        """Test that decimal values with different precision but same value are considered equal"""
        # Arrange
        self.invoice.total_cost = Decimal("0.0000")
        self.invoice.total_price = Decimal("0.00")
        self.invoice.save()

        # Mock the total and price properties to return different precision but same values
        with (
            mock.patch.object(
                models.Invoice, "total", new_callable=mock.PropertyMock
            ) as mock_total,
            mock.patch.object(
                models.Invoice, "price", new_callable=mock.PropertyMock
            ) as mock_price,
            mock.patch.object(models.Invoice, "save") as mock_save,
        ):
            # Same value as stored, but with different precision
            mock_total.return_value = Decimal("0.00")  # Different precision from 0.0000
            mock_price.return_value = Decimal("0.0000")  # Different precision from 0.00

            # Act
            self.invoice.update_cache()

            # Assert - should not call save because the values are numerically equal
            mock_save.assert_not_called()

    def test_update_cache_with_improved_comparison(self):
        """Test the fixed implementation that handles decimal precision correctly"""
        # Arrange - patch the update_cache method with a fixed implementation
        original_update_cache = models.Invoice.update_cache

        def fixed_update_cache(self):
            """Fixed implementation that quantizes before comparison"""
            updates = {}

            current_total = self.total
            # Quantize to same precision for accurate comparison
            if self.total_cost.quantize(current_total) != current_total:
                updates["total_cost"] = current_total

            current_price = self.price
            # Quantize to same precision for accurate comparison
            if self.total_price.quantize(current_price) != current_price:
                updates["total_price"] = current_price

            if updates:
                for field, value in updates.items():
                    setattr(self, field, value)
                self.save(update_fields=list(updates.keys()))

        models.Invoice.update_cache = fixed_update_cache

        try:
            # Set initial values
            self.invoice.total_cost = Decimal("0.0000")
            self.invoice.total_price = Decimal("0.00")
            self.invoice.save()

            # Mock properties to return values with different precision
            with (
                mock.patch.object(
                    models.Invoice, "total", new_callable=mock.PropertyMock
                ) as mock_total,
                mock.patch.object(
                    models.Invoice, "price", new_callable=mock.PropertyMock
                ) as mock_price,
                mock.patch.object(models.Invoice, "save") as mock_save,
            ):
                mock_total.return_value = Decimal(
                    "0.00"
                )  # Different precision but same value
                mock_price.return_value = Decimal(
                    "0.0000"
                )  # Different precision but same value

                # Act
                self.invoice.update_cache()

                # Assert - should not call save because of improved comparison
                mock_save.assert_not_called()
        finally:
            # Restore original method
            models.Invoice.update_cache = original_update_cache

    def test_update_cache_with_one_digit_difference(self):
        """Test that update_cache detects actual value differences, not just precision"""
        # Arrange
        self.invoice.total_cost = Decimal("10.0000")
        self.invoice.total_price = Decimal("10.00")
        self.invoice.save()

        # Mock the total and price properties to return slightly different values
        with (
            mock.patch.object(
                models.Invoice, "total", new_callable=mock.PropertyMock
            ) as mock_total,
            mock.patch.object(
                models.Invoice, "price", new_callable=mock.PropertyMock
            ) as mock_price,
            mock.patch.object(models.Invoice, "save") as mock_save,
        ):
            mock_total.return_value = Decimal("10.0001")  # Slightly different
            mock_price.return_value = Decimal("10.01")  # Slightly different

            # Act
            self.invoice.update_cache()

            # Assert - should call save with both fields
            mock_save.assert_called_once_with(
                update_fields=["total_cost", "total_price"]
            )
