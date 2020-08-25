import datetime
from decimal import Decimal
from unittest import mock

from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.media.utils import dummy_image
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import PACKAGE_TYPE
from waldur_mastermind.packages.tests import fixtures as packages_fixtures
from waldur_mastermind.packages.tests.utils import override_plugin_settings
from waldur_mastermind.slurm_invoices.tests import factories as slurm_factories
from waldur_mastermind.support.tests import fixtures as support_fixtures
from waldur_slurm.tests import fixtures as slurm_fixtures

from .. import models, tasks, utils
from . import factories, fixtures
from . import utils as test_utils


@ddt
class InvoiceRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    @data('owner', 'staff')
    def test_user_with_access_can_retrieve_customer_invoice(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_retrieve_customer_invoice(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class InvoiceSendNotificationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = factories.InvoiceFactory.get_url(
            self.fixture.invoice, action='send_notification'
        )
        self.fixture.invoice.state = models.Invoice.States.CREATED
        self.fixture.invoice.save(update_fields=['state'])

        self.patcher = mock.patch('waldur_mastermind.invoices.utils.pdfkit')
        mock_pdfkit = self.patcher.start()
        mock_pdfkit.from_string.return_value = b'PDF'

    def tearDown(self):
        super(InvoiceSendNotificationTest, self).tearDown()
        mock.patch.stopall()

    @data('staff')
    def test_user_can_send_invoice_notification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_send_invoice_notification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(task_always_eager=True)
    @test_utils.override_invoices_settings(
        INVOICE_LINK_TEMPLATE='http://example.com/invoice/{uuid}'
    )
    def test_notification_email_is_rendered(self):
        # Arrange
        self.fixture.owner

        # Act
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.url)

        # Assert
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue('invoice' in mail.outbox[0].subject)
        self.assertEqual(self.fixture.owner.email, mail.outbox[0].to[0])

    @override_settings(task_always_eager=True)
    @test_utils.override_invoices_settings(
        INVOICE_LINK_TEMPLATE='http://example.com/invoice/'
    )
    def test_user_cannot_send_invoice_notification_with_invalid_link_template(self):
        # Arrange
        self.fixture.owner

        # Act
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.url)

        # Assert
        self.assertEqual(len(mail.outbox), 0)

    def test_user_cannot_send_invoice_notification_in_invalid_state(self):
        self.fixture.invoice.state = models.Invoice.States.PENDING
        self.fixture.invoice.save(update_fields=['state'])
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data, ["Notification only for the created invoice can be sent."]
        )


@override_plugin_settings(BILLING_ENABLED=True)
class UpdateInvoiceItemProjectTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.package = self.fixture.openstack_package

    def test_project_name_and_uuid_is_rendered_for_invoice_item(self):
        self.check_invoice_item()

    def test_when_project_is_deleted_invoice_item_is_present(self):
        self.fixture.openstack_tenant.delete()
        self.fixture.project.delete()
        self.check_invoice_item()

    def test_when_project_is_updated_invoice_item_is_synced(self):
        self.fixture.project.name = 'New name'
        self.fixture.project.save()
        self.check_invoice_item()

    def test_invoice_item_updated_for_pending_invoice_only(self):
        self.invoice.state = models.Invoice.States.CANCELED
        self.invoice.save()
        old_name = self.fixture.project.name
        self.fixture.project.name = 'New name'
        self.fixture.project.save()
        self.check_invoice_item(old_name)

    def check_invoice_item(self, project_name=None):
        if project_name is None:
            project_name = self.fixture.project.name

        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(factories.InvoiceFactory.get_url(self.invoice))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item = response.data['items'][0]
        self.assertEqual(item['project_name'], project_name)
        self.assertEqual(item['project_uuid'], self.fixture.project.uuid.hex)


@override_plugin_settings(BILLING_ENABLED=True)
class OpenStackInvoiceItemTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.item = models.InvoiceItem.objects.get(scope=self.package)

    def check_output(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.InvoiceFactory.get_url(self.item.invoice))
        item = response.data['items'][0]
        self.assertEqual(item['details']['tenant_name'], self.package.tenant.name)
        self.assertEqual(item['details']['tenant_uuid'], self.package.tenant.uuid.hex)
        self.assertEqual(item['details']['template_name'], self.package.template.name)
        self.assertEqual(
            item['details']['template_uuid'], self.package.template.uuid.hex
        )
        self.assertEqual(
            item['details']['template_category'],
            self.package.template.get_category_display(),
        )

    def test_details_are_rendered_if_package_exists(self):
        self.check_output()

    def test_details_are_rendered_if_package_has_been_deleted(self):
        self.package.delete()
        self.item.refresh_from_db()
        self.check_output()


class InvoiceItemTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = slurm_fixtures.SlurmFixture()
        self.package = slurm_factories.SlurmPackageFactory(
            service_settings=self.fixture.service.settings
        )
        self.invoice = factories.InvoiceFactory(customer=self.fixture.customer)
        self.scope = self.fixture.allocation
        self.usage = self.fixture.allocation_usage
        self.items = models.InvoiceItem.objects.filter(scope=self.scope)
        for item in self.items:
            item.unit = models.InvoiceItem.Units.QUANTITY
            item.quantity = 10
            item.unit_price = 10
            item.save()

    def check_output(self):
        self.client.force_authenticate(self.fixture.owner)
        invoice = self.items[0].invoice
        response = self.client.get(factories.InvoiceFactory.get_url(invoice))
        response_items = response.data['items']
        self.assertNotEqual(len(response_items), 0)
        for response_item in response_items:
            self.assertEqual(response_item['scope_type'], 'SLURM.Allocation')
            self.assertEqual(response_item['scope_uuid'], self.scope.uuid.hex)
            self.assertTrue(self.scope.name in response_item['name'])

    def test_details_are_rendered_if_scope_exists(self):
        self.check_output()

    def test_details_are_rendered_if_scope_has_been_deleted(self):
        self.scope.delete()
        for item in self.items:
            item.refresh_from_db()
        self.check_output()

    def test_scope_type_is_rendered_for_support_request(self):
        fixture = support_fixtures.SupportFixture()
        invoice = factories.InvoiceFactory(customer=fixture.customer)
        models.InvoiceItem.objects.create(
            scope=fixture.offering,
            invoice=invoice,
            unit=models.InvoiceItem.Units.QUANTITY,
            quantity=10,
            unit_price=10,
        )
        url = factories.InvoiceFactory.get_url(invoice)

        self.client.force_authenticate(fixture.owner)
        response = self.client.get(url)
        item = response.data['items'][0]
        self.assertEqual(item['scope_type'], 'Support.Offering')


class InvoiceStatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.provider = marketplace_factories.ServiceProviderFactory()
        self.provider_2 = marketplace_factories.ServiceProviderFactory()

        self.offering = marketplace_factories.OfferingFactory(
            type=PACKAGE_TYPE, customer=self.provider.customer
        )

        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.plan = marketplace_factories.PlanFactory(
            offering=self.offering, unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.component = marketplace_factories.PlanComponentFactory(
            component=self.offering_component, price=Decimal(5), plan=self.plan
        )

        self.offering_2 = marketplace_factories.OfferingFactory(
            type=PACKAGE_TYPE, customer=self.provider_2.customer
        )

        self.offering_component_2 = marketplace_factories.OfferingComponentFactory(
            offering=self.offering_2
        )
        self.plan_2 = marketplace_factories.PlanFactory(
            offering=self.offering_2, unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.component_2 = marketplace_factories.PlanComponentFactory(
            component=self.offering_component_2, price=Decimal(7), plan=self.plan_2
        )

        self.resource_1 = marketplace_factories.ResourceFactory(
            state=marketplace_models.Resource.States.OK,
            offering=self.offering,
            plan=self.plan,
            limits={'cpu': 1},
        )

        self.resource_2 = marketplace_factories.ResourceFactory(
            state=marketplace_models.Resource.States.OK,
            offering=self.offering,
            project=self.resource_1.project,
            plan=self.plan,
            limits={'cpu': 1},
        )

        self.resource_3 = marketplace_factories.ResourceFactory(
            state=marketplace_models.Resource.States.OK,
            offering=self.offering_2,
            project=self.resource_1.project,
            plan=self.plan_2,
            limits={'cpu': 1},
        )

        self.customer = self.resource_1.project.customer

    @freeze_time('2019-01-01')
    def test_invoice_stats(self):
        tasks.create_monthly_invoices()
        invoice = models.Invoice.objects.get(customer=self.customer)
        url = factories.InvoiceFactory.get_url(invoice=invoice, action='stats')
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        result = self.client.get(url)
        self.assertEqual(
            result.data,
            {
                self.offering.uuid.hex: {
                    'offering_name': self.offering.name,
                    'aggregated_cost': float(
                        sum(
                            [
                                item.total
                                for item in models.InvoiceItem.objects.filter(
                                    invoice=invoice,
                                    object_id__in=[
                                        self.resource_1.id,
                                        self.resource_2.id,
                                    ],
                                )
                            ]
                        )
                    ),
                    'service_category_title': self.offering.category.title,
                    'service_provider_name': self.offering.customer.name,
                    'service_provider_uuid': self.provider.uuid.hex,
                },
                self.offering_2.uuid.hex: {
                    'offering_name': self.offering_2.name,
                    'aggregated_cost': float(
                        sum(
                            [
                                item.total
                                for item in models.InvoiceItem.objects.filter(
                                    invoice=invoice, object_id__in=[self.resource_3.id]
                                )
                            ]
                        )
                    ),
                    'service_category_title': self.offering_2.category.title,
                    'service_provider_name': self.offering_2.customer.name,
                    'service_provider_uuid': self.provider_2.uuid.hex,
                },
            },
        )


class DeleteCustomerWithInvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = slurm_fixtures.SlurmFixture()
        self.invoice = factories.InvoiceFactory(customer=self.fixture.customer)
        self.url = structure_factories.CustomerFactory.get_url(self.fixture.customer)

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_owner_can_delete_customer_with_pending_invoice(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_owner_can_not_delete_customer_with_non_empty_invoice(self):
        factories.InvoiceItemFactory(invoice=self.invoice, unit_price=100)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_owner_can_not_delete_customer_with_active_invoice_even_if_its_empty(self):
        self.invoice.state = models.Invoice.States.CREATED
        self.invoice.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
class InvoicePDFTest(test.APITransactionTestCase):
    def setUp(self):
        self.invoice = factories.InvoiceFactory()

    @mock.patch('waldur_mastermind.invoices.utils.pdfkit')
    def test_create_invoice_pdf(self, mock_pdfkit):
        mock_pdfkit.from_string.return_value = b'pdf_content'
        utils.create_invoice_pdf(self.invoice)
        self.assertTrue(self.invoice.has_file())

    @mock.patch('waldur_mastermind.invoices.handlers.tasks')
    def test_create_invoice_pdf_is_not_called_if_invoice_cost_has_not_been_changed(
        self, mock_tasks
    ):
        with freeze_time('2019-01-02'):
            invoice = factories.InvoiceFactory()
            factories.InvoiceItemFactory(invoice=invoice, unit_price=Decimal(10))
            self.assertEqual(mock_tasks.create_invoice_pdf.delay.call_count, 1)
            invoice.update_current_cost()
            self.assertEqual(mock_tasks.create_invoice_pdf.delay.call_count, 1)

    @mock.patch('waldur_mastermind.invoices.handlers.tasks')
    def test_create_invoice_pdf_is_called_if_invoice_cost_has_been_changed(
        self, mock_tasks
    ):
        with freeze_time('2019-01-02'):
            invoice = factories.InvoiceFactory()
            factories.InvoiceItemFactory(invoice=invoice, unit_price=Decimal(10))
            self.assertEqual(mock_tasks.create_invoice_pdf.delay.call_count, 1)
            factories.InvoiceItemFactory(invoice=invoice, unit_price=Decimal(10))
            invoice.update_current_cost()
            self.assertEqual(mock_tasks.create_invoice_pdf.delay.call_count, 2)


@ddt
class InvoicePaidTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.invoice.state = models.Invoice.States.CREATED
        self.invoice.save()
        self.url = factories.InvoiceFactory.get_url(self.invoice, 'paid')

    def test_staff_can_mark_invoice_as_paid(self):
        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, models.Invoice.States.PAID)

    @data('owner', 'manager', 'admin', 'user')
    def test_other_users_cannot_mark_invoice_as_paid(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_mark_invoice_as_paid_if_current_state_is_not_created(self):
        self.invoice.state = models.Invoice.States.PENDING
        self.invoice.save()
        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_create_payment_if_payment_data_has_been_passed(self):
        profile = factories.PaymentProfileFactory(
            organization=self.invoice.customer, is_active=True
        )

        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        date = datetime.date.today()
        response = self.client.post(
            self.url, data={'date': date, 'proof': dummy_image()}, format='multipart'
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
        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        date = datetime.date.today()
        response = self.client.post(
            self.url, data={'date': date, 'proof': dummy_image()}, format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_proof_is_not_required_for_payment_of_invoice(self):
        factories.PaymentProfileFactory(
            organization=self.invoice.customer, is_active=True
        )

        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        date = datetime.date.today()
        response = self.client.post(self.url, data={'date': date}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
