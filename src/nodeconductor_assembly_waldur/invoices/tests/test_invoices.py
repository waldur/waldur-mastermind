from ddt import ddt, data
from rest_framework import test, status

from nodeconductor_assembly_waldur.invoices import models, serializers
from nodeconductor_assembly_waldur.packages.tests import fixtures as packages_fixtures

from . import factories, fixtures
from .. import models


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

        item = response.data['openstack_items'][0]
        self.assertEqual(item['project_name'], project_name)
        self.assertEqual(item['project_uuid'], self.fixture.project.uuid.hex)


class OpenStackInvoiceItemTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.item = models.OpenStackItem.objects.get(package=self.package)

    def check_output(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.InvoiceFactory.get_url(self.item.invoice))
        item = response.data['openstack_items'][0]
        self.assertEqual(item['tenant_name'], self.package.tenant.name)
        self.assertEqual(item['tenant_uuid'], self.package.tenant.uuid.hex)
        self.assertEqual(item['template_name'], self.package.template.name)
        self.assertEqual(item['template_uuid'], self.package.template.uuid.hex)

    def test_details_are_rendered_if_package_exists(self):
        self.check_output()

    def test_details_are_rendered_if_package_has_been_deleted(self):
        self.package.delete()
        self.item.refresh_from_db()
        self.check_output()
