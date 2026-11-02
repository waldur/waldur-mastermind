from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class InvoiceListPermissionTest(test.APITestCase):
    """An organization role sees the organization's invoices and their items
    only when it carries INVOICE.LIST; holding some role on the organization is
    not enough."""

    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item
        self.user = structure_factories.UserFactory()

    def list_invoices(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(factories.InvoiceFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [invoice["uuid"] for invoice in response.data]

    def retrieve_invoice(self):
        self.client.force_authenticate(self.user)
        return self.client.get(factories.InvoiceFactory.get_url(self.invoice))

    def list_invoice_items(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(factories.InvoiceItemFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["uuid"] for item in response.data]

    def create_custom_role(self):
        return Role.objects.create(
            name="CUSTOMER.REPRESENTATIVE",
            content_type=ContentType.objects.get_for_model(Customer),
        )

    def test_custom_role_with_permission_sees_invoices(self):
        role = self.create_custom_role()
        role.add_permission(PermissionEnum.LIST_INVOICES)
        self.fixture.customer.add_user(self.user, role)

        self.assertEqual(self.list_invoices(), [self.invoice.uuid.hex])
        self.assertEqual(self.retrieve_invoice().status_code, status.HTTP_200_OK)
        self.assertEqual(self.list_invoice_items(), [self.invoice_item.uuid.hex])

    def test_custom_role_without_permission_sees_no_invoices(self):
        self.fixture.customer.add_user(self.user, self.create_custom_role())

        self.assertEqual(self.list_invoices(), [])
        self.assertEqual(self.list_invoice_items(), [])
        self.assertEqual(self.retrieve_invoice().status_code, status.HTTP_404_NOT_FOUND)

    def test_reader_without_permission_sees_no_invoices(self):
        self.fixture.customer.add_user(self.user, CustomerRole.READER)

        self.assertEqual(self.list_invoices(), [])
        self.assertEqual(self.retrieve_invoice().status_code, status.HTTP_404_NOT_FOUND)

    def test_permission_on_another_organization_does_not_leak(self):
        role = self.create_custom_role()
        role.add_permission(PermissionEnum.LIST_INVOICES)
        structure_factories.CustomerFactory().add_user(self.user, role)

        self.assertEqual(self.list_invoices(), [])

    def test_staff_sees_invoices_without_any_role(self):
        self.user = self.fixture.staff

        self.assertEqual(self.list_invoices(), [self.invoice.uuid.hex])

    def test_project_member_still_sees_items_of_own_project(self):
        # Project roles reach items through display_billing_info_in_projects,
        # not through INVOICE.LIST, and never reach the invoice itself.
        self.user = self.fixture.member

        self.assertEqual(self.list_invoice_items(), [self.invoice_item.uuid.hex])
        self.assertEqual(self.list_invoices(), [])

    def test_project_member_sees_no_items_when_billing_info_is_concealed(self):
        self.fixture.customer.display_billing_info_in_projects = False
        self.fixture.customer.save(update_fields=["display_billing_info_in_projects"])
        self.user = self.fixture.member

        self.assertEqual(self.list_invoice_items(), [])

    def test_project_member_without_permission_sees_only_own_project_items(self):
        # Both access paths at once: an organization role without INVOICE.LIST
        # must not widen what the project role reaches.
        self.user = self.fixture.member
        self.fixture.customer.add_user(self.user, self.create_custom_role())
        other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        factories.InvoiceItemFactory(invoice=self.invoice, project=other_project)

        self.assertEqual(self.list_invoice_items(), [self.invoice_item.uuid.hex])
        self.assertEqual(self.list_invoices(), [])
        self.assertEqual(self.retrieve_invoice().status_code, status.HTTP_404_NOT_FOUND)

    def test_service_provider_manager_sees_no_invoices(self):
        # The manager role is held on the ServiceProvider, never on the
        # organization, so the invoice querysets do not reach it.
        service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        service_provider.add_user(self.user, ServiceProviderRole.MANAGER)

        self.assertEqual(self.list_invoices(), [])
        self.assertEqual(self.list_invoice_items(), [])
        self.assertEqual(self.retrieve_invoice().status_code, status.HTTP_404_NOT_FOUND)
