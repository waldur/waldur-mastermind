from decimal import Decimal
from ddt import ddt, data

from django.db.models import signals
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test, status

from nodeconductor.core import utils as core_utils
from nodeconductor_assembly_waldur.packages import models as packages_models
from nodeconductor_assembly_waldur.packages.tests import factories as packages_factories

from . import factories, fixtures
from .. import handlers
from .. import models
from .. import utils


@ddt
class InvoiceRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    @data('owner', 'staff')
    def test_user_with_access_can_retrieve_customer_invoice(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.InvoiceFactory.get_url(self.fixture.invoice))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_retrieve_customer_invoice(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.InvoiceFactory.get_url(self.fixture.invoice))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class InvoiceSendNotificationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = factories.InvoiceFactory.get_url(self.fixture.invoice, action='send_notification')
        self.fixture.invoice.state = models.Invoice.States.CREATED
        self.fixture.invoice.save(update_fields=['state'])

    @data('staff')
    def test_user_can_send_invoice_notification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_send_invoice_notification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_send_invoice_notification_with_invalid_link_template(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload()
        payload['link_template'] = 'http://example.com/invoice/'

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['link_template'], ["Link template must include '{uuid}' parameter."])

    def test_user_cannot_send_invoice_notification_in_invalid_state(self):
        self.fixture.invoice.state = models.Invoice.States.PENDING
        self.fixture.invoice.save(update_fields=['state'])
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, ["Notification only for the created invoice can be sent."])

    # Helper methods
    def _get_payload(self):
        return {
            'link_template': 'http://example.com/invoice/{uuid}',
        }


class InvoiceTotalPriceUpdateTest(test.APITestCase):

    def setUp(self):
        signals.pre_delete.disconnect(
            receiver=handlers.update_invoice_on_openstack_package_deletion,
            sender=packages_models.OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.update_invoice_on_openstack_package_deletion')

    @freeze_time("2016-10-14")
    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper(self):

        # arrange
        base_component_price = 10
        advanced_component_price = base_component_price + 5
        day_before_package_changed = timezone.now() + timezone.timedelta(days=5)
        day_to_change_package = day_before_package_changed + timezone.timedelta(days=1)

        # set up base package
        base_package_template = packages_factories.PackageTemplateFactory()
        first_component = base_package_template.components.first()
        first_component.price = base_component_price
        first_component.amount = 1
        first_component.save()
        old_package = packages_factories.OpenStackPackageFactory(template=base_package_template)
        self.assertEqual(models.OpenStackItem.objects.count(), 1)
        old_item = models.OpenStackItem.objects.first()
        old_item.freeze(end=day_to_change_package, package_deletion=True)
        customer = old_package.tenant.service_project_link.project.customer
        old_package.delete()

        # advanced package
        advanced_package_template = packages_factories.PackageTemplateFactory()
        advanced_component = advanced_package_template.components.first()
        advanced_component.price = advanced_component_price
        advanced_component.amount = 1
        advanced_component.save()

        with freeze_time(time_to_freeze=day_to_change_package):
            packages_factories.OpenStackPackageFactory(
                template=advanced_package_template,
                tenant__service_project_link__project__customer=customer,
            )

        expected_price = models.OpenStackItem.calculate_price_for_period(
            price=base_component_price,
            start=timezone.now(),
            end=day_before_package_changed
        ) + models.OpenStackItem.calculate_price_for_period(
            price=advanced_component_price,
            start=day_to_change_package,
            end=utils.get_current_month_end()
        )

        # assert
        invoices_price = reduce(lambda previous, invoice: previous + invoice.price, models.Invoice.objects.all(), 0)
        self.assertEqual(Decimal(expected_price), invoices_price)

    @freeze_time("2016-10-14")
    def test_package_creation_increases_price_from_old_package_if_it_is_more_expensive(self):

        # arrange
        base_component_price = 10
        advanced_component_price = base_component_price - 5
        day_before_package_changed = timezone.now() + timezone.timedelta(days=5)
        day_to_change_package = day_before_package_changed + timezone.timedelta(days=1)

        # set up base package
        base_package_template = packages_factories.PackageTemplateFactory()
        first_component = base_package_template.components.first()
        first_component.price = base_component_price
        first_component.amount = 1
        first_component.save()
        old_package = packages_factories.OpenStackPackageFactory(template=base_package_template)
        self.assertEqual(models.OpenStackItem.objects.count(), 1)
        old_item = models.OpenStackItem.objects.first()
        old_item.freeze(end=day_to_change_package, package_deletion=True)
        customer = old_package.tenant.service_project_link.project.customer
        old_package.delete()

        # advanced package
        advanced_package_template = packages_factories.PackageTemplateFactory()
        advanced_component = advanced_package_template.components.first()
        advanced_component.price = advanced_component_price
        advanced_component.amount = 1
        advanced_component.save()

        with freeze_time(time_to_freeze=day_to_change_package):
            packages_factories.OpenStackPackageFactory(
                template=advanced_package_template,
                tenant__service_project_link__project__customer=customer,
            )

        expected_price = models.OpenStackItem.calculate_price_for_period(
            price=base_component_price,
            start=timezone.now(),
            end=day_to_change_package
        ) + models.OpenStackItem.calculate_price_for_period(
            price=advanced_component_price,
            start=day_to_change_package + timezone.timedelta(days=1),
            end=utils.get_current_month_end()
        )

        # assert
        invoices_price = reduce(lambda previous, invoice: previous + invoice.price, models.Invoice.objects.all(), 0)
        self.assertEqual(Decimal(expected_price), invoices_price)

    @freeze_time("2014-2-20")
    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper_in_the_end_of_the_month(self):

        # arrange
        base_component_price = 10
        advanced_component_price = base_component_price + 5
        day_before_package_changed = timezone.now() + timezone.timedelta(days=7)

        # day to change package is 28th February 2014
        day_to_change_package = day_before_package_changed + timezone.timedelta(days=1)

        # set up base package
        base_package_template = packages_factories.PackageTemplateFactory()
        first_component = base_package_template.components.first()
        first_component.price = base_component_price
        first_component.amount = 1
        first_component.save()
        old_package = packages_factories.OpenStackPackageFactory(template=base_package_template)
        self.assertEqual(models.OpenStackItem.objects.count(), 1)
        old_item = models.OpenStackItem.objects.first()
        old_item.freeze(end=day_to_change_package, package_deletion=True)
        customer = old_package.tenant.service_project_link.project.customer
        old_package.delete()

        # advanced package
        advanced_package_template = packages_factories.PackageTemplateFactory()
        advanced_component = advanced_package_template.components.first()
        advanced_component.price = advanced_component_price
        advanced_component.amount = 1
        advanced_component.save()

        with freeze_time(time_to_freeze=day_to_change_package):
            packages_factories.OpenStackPackageFactory(
                template=advanced_package_template,
                tenant__service_project_link__project__customer=customer,
            )

        expected_price = models.OpenStackItem.calculate_price_for_period(
            price=base_component_price,
            start=timezone.now(),
            end=day_before_package_changed
        ) + models.OpenStackItem.calculate_price_for_period(
            price=advanced_component_price,
            start=day_to_change_package,
            end=utils.get_current_month_end()
        )

        # assert
        invoices_price = reduce(lambda previous, invoice: previous + invoice.price, models.Invoice.objects.all(), 0)
        self.assertEqual(Decimal(expected_price), invoices_price)

    @freeze_time("2014-2-20")
    def test_package_creation_increases_price_from_old_package_if_it_is_more_expensive_in_the_end_of_the_month(self):

        # arrange
        base_component_price = 15
        advanced_component_price = base_component_price - 5
        day_before_package_changed = timezone.now() + timezone.timedelta(days=7)

        # day to change package is 28th February 2014
        day_to_change_package = day_before_package_changed + timezone.timedelta(days=1)

        # set up base package
        base_package_template = packages_factories.PackageTemplateFactory()
        first_component = base_package_template.components.first()
        first_component.price = base_component_price
        first_component.amount = 1
        first_component.save()
        old_package = packages_factories.OpenStackPackageFactory(template=base_package_template)
        self.assertEqual(models.OpenStackItem.objects.count(), 1)
        old_item = models.OpenStackItem.objects.first()
        old_item.freeze(end=day_to_change_package, package_deletion=True)
        customer = old_package.tenant.service_project_link.project.customer
        old_package.delete()

        # advanced package
        advanced_package_template = packages_factories.PackageTemplateFactory()
        advanced_component = advanced_package_template.components.first()
        advanced_component.price = advanced_component_price
        advanced_component.amount = 1
        advanced_component.save()

        with freeze_time(time_to_freeze=day_to_change_package):
            packages_factories.OpenStackPackageFactory(
                template=advanced_package_template,
                tenant__service_project_link__project__customer=customer,
            )

        expected_price = models.OpenStackItem.calculate_price_for_period(
            price=base_component_price,
            start=timezone.now(),
            end=day_to_change_package,
        ) + models.OpenStackItem.calculate_price_for_period(
            price=advanced_component_price,
            start=day_to_change_package+timezone.timedelta(days=1),
            end=core_utils.month_end(day_to_change_package+timezone.timedelta(days=1))
        )

        # assert
        invoices_price = reduce(lambda previous, invoice: previous + invoice.price, models.Invoice.objects.all(), 0)
        self.assertEqual(Decimal(expected_price), invoices_price)

    @freeze_time("2014-2-20")
    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper_in_the_start_of_the_month(self):

        # arrange
        base_component_price = 10
        advanced_component_price = base_component_price + 5
        day_before_package_changed = timezone.now() + timezone.timedelta(days=8)

        # day to change package is 28th February 2014
        day_to_change_package = day_before_package_changed + timezone.timedelta(days=1)

        # set up base package
        base_package_template = packages_factories.PackageTemplateFactory()
        first_component = base_package_template.components.first()
        first_component.price = base_component_price
        first_component.amount = 1
        first_component.save()
        old_package = packages_factories.OpenStackPackageFactory(template=base_package_template)
        self.assertEqual(models.OpenStackItem.objects.count(), 1)
        old_item = models.OpenStackItem.objects.first()
        old_item.freeze(end=day_to_change_package, package_deletion=True)
        customer = old_package.tenant.service_project_link.project.customer
        old_package.delete()

        # advanced package
        advanced_package_template = packages_factories.PackageTemplateFactory()
        advanced_component = advanced_package_template.components.first()
        advanced_component.price = advanced_component_price
        advanced_component.amount = 1
        advanced_component.save()

        with freeze_time(time_to_freeze=day_to_change_package):
            packages_factories.OpenStackPackageFactory(
                template=advanced_package_template,
                tenant__service_project_link__project__customer=customer,
            )

        expected_price = models.OpenStackItem.calculate_price_for_period(
            price=advanced_component_price,
            start=day_to_change_package,
            end=core_utils.month_end(day_to_change_package+timezone.timedelta(days=1))
        )

        # assert
        invoices_price = reduce(lambda previous, invoice: previous + invoice.price,
                                models.Invoice.objects.filter(month=day_to_change_package.month).all(), 0)
        self.assertEqual(Decimal(expected_price), invoices_price)

