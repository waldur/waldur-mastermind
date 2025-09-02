from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class InvoiceItemsOrderingTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

        # Create additional projects for testing project name ordering
        self.project_a = structure_factories.ProjectFactory(
            customer=self.fixture.customer, name="Alpha Project"
        )
        self.project_z = structure_factories.ProjectFactory(
            customer=self.fixture.customer, name="Zeta Project"
        )

        # Create additional providers for testing provider name ordering
        self.provider_a = structure_factories.CustomerFactory(name="Alpha Provider")
        self.provider_z = structure_factories.CustomerFactory(name="Zeta Provider")

        # Create offerings and resources for different providers
        self.offering_a = marketplace_factories.OfferingFactory(
            customer=self.provider_a, name="Alpha Offering"
        )
        self.offering_z = marketplace_factories.OfferingFactory(
            customer=self.provider_z, name="Zeta Offering"
        )

        self.resource_a = marketplace_factories.ResourceFactory(
            offering=self.offering_a, project=self.project_a, name="Alpha Resource"
        )
        self.resource_z = marketplace_factories.ResourceFactory(
            offering=self.offering_z, project=self.project_z, name="Zeta Resource"
        )

        # Create invoice items with different project names, resource names, and providers
        self.item_alpha = factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.project_a,
            project_name="Alpha Project",
            resource=self.resource_a,
            name="Alpha Item",
            unit_price=10,
            quantity=1,
            details={
                "service_provider_name": "Alpha Provider",
                "service_provider_uuid": str(self.provider_a.uuid),
                "offering_name": "Alpha Offering",
                "offering_uuid": str(self.offering_a.uuid),
            },
        )

        self.item_zeta = factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.project_z,
            project_name="Zeta Project",
            resource=self.resource_z,
            name="Zeta Item",
            unit_price=20,
            quantity=1,
            details={
                "service_provider_name": "Zeta Provider",
                "service_provider_uuid": str(self.provider_z.uuid),
                "offering_name": "Zeta Offering",
                "offering_uuid": str(self.offering_z.uuid),
            },
        )

        self.items_url = factories.InvoiceFactory.get_url(self.fixture.invoice, "items")

    def get_items(self, user, ordering=None):
        self.client.force_authenticate(user)
        params = {}
        if ordering:
            params["o"] = ordering
        return self.client.get(self.items_url, params)

    def test_staff_can_access_items_endpoint(self):
        response = self.get_items(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_ordering_by_project_name_ascending(self):
        response = self.get_items(self.fixture.staff, ordering="project_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered alphabetically: Alpha Project, Zeta Project
        self.assertEqual(items[0]["project_name"], "Alpha Project")
        self.assertEqual(items[1]["project_name"], "Zeta Project")

    def test_ordering_by_project_name_descending(self):
        response = self.get_items(self.fixture.staff, ordering="-project_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered reverse alphabetically: Zeta Project, Alpha Project
        self.assertEqual(items[0]["project_name"], "Zeta Project")
        self.assertEqual(items[1]["project_name"], "Alpha Project")

    def test_ordering_by_resource_name_ascending(self):
        response = self.get_items(self.fixture.staff, ordering="resource_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered alphabetically: Alpha Resource, Zeta Resource
        self.assertEqual(items[0]["resource_name"], "Alpha Resource")
        self.assertEqual(items[1]["resource_name"], "Zeta Resource")

    def test_ordering_by_resource_name_descending(self):
        response = self.get_items(self.fixture.staff, ordering="-resource_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered reverse alphabetically: Zeta Resource, Alpha Resource
        self.assertEqual(items[0]["resource_name"], "Zeta Resource")
        self.assertEqual(items[1]["resource_name"], "Alpha Resource")

    def test_ordering_by_provider_name_ascending(self):
        response = self.get_items(self.fixture.staff, ordering="provider_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered alphabetically by service provider name in details
        # Get provider names from details field
        provider_names = [item["details"]["service_provider_name"] for item in items]
        self.assertEqual(provider_names[0], "Alpha Provider")
        self.assertEqual(provider_names[1], "Zeta Provider")

    def test_ordering_by_provider_name_descending(self):
        response = self.get_items(self.fixture.staff, ordering="-provider_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered reverse alphabetically by service provider name in details
        provider_names = [item["details"]["service_provider_name"] for item in items]
        self.assertEqual(provider_names[0], "Zeta Provider")
        self.assertEqual(provider_names[1], "Alpha Provider")

    def test_ordering_by_name_ascending(self):
        response = self.get_items(self.fixture.staff, ordering="name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered alphabetically: Alpha Item, Zeta Item
        self.assertEqual(items[0]["name"], "Alpha Item")
        self.assertEqual(items[1]["name"], "Zeta Item")

    def test_ordering_by_name_descending(self):
        response = self.get_items(self.fixture.staff, ordering="-name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)

        # Should be ordered reverse alphabetically: Zeta Item, Alpha Item
        self.assertEqual(items[0]["name"], "Zeta Item")
        self.assertEqual(items[1]["name"], "Alpha Item")

    def test_no_ordering_parameter_returns_items_without_ordering(self):
        response = self.get_items(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)
        # Without ordering, items should be returned in default order

    def test_invalid_ordering_parameter_is_ignored(self):
        response = self.get_items(self.fixture.staff, ordering="invalid_field")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 2)
        # Invalid ordering should not cause errors, just return unordered

    def test_ordering_with_null_values(self):
        # Create an item with empty project name but valid project to avoid serializer issues
        factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,  # Use a valid project to avoid serializer errors
            project_name="",  # Empty project name for ordering test
            resource=self.fixture.resource,  # Use valid resource
            name="Null Item",
            unit_price=30,
            quantity=1,
        )

        response = self.get_items(self.fixture.staff, ordering="project_name")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 3)
        # Items with null/empty project names should be handled gracefully
        # Empty project name should appear first or last depending on null ordering


class InvoiceItemsOrderingWithFiltersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

        # Create projects with specific names
        self.project_a = structure_factories.ProjectFactory(
            customer=self.fixture.customer, name="Alpha Project"
        )
        self.project_b = structure_factories.ProjectFactory(
            customer=self.fixture.customer, name="Beta Project"
        )

        # Create resources
        self.resource_a = marketplace_factories.ResourceFactory(
            project=self.project_a, name="Alpha Resource"
        )
        self.resource_b = marketplace_factories.ResourceFactory(
            project=self.project_b, name="Beta Resource"
        )

        # Create invoice items
        self.item_a = factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.project_a,
            project_name="Alpha Project",
            resource=self.resource_a,
            unit_price=10,
            quantity=1,
        )

        self.item_b = factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.project_b,
            project_name="Beta Project",
            resource=self.resource_b,
            unit_price=20,
            quantity=1,
        )

        self.items_url = factories.InvoiceFactory.get_url(self.fixture.invoice, "items")

    def get_items(self, user, ordering=None, project_uuid=None):
        self.client.force_authenticate(user)
        params = {}
        if ordering:
            params["o"] = ordering
        if project_uuid:
            params["project_uuid"] = project_uuid
        return self.client.get(self.items_url, params)

    def test_ordering_combined_with_project_filter(self):
        # Test that ordering works when combined with project filtering
        response = self.get_items(
            self.fixture.staff,
            ordering="-project_name",
            project_uuid=str(self.project_a.uuid),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["project_name"], "Alpha Project")
