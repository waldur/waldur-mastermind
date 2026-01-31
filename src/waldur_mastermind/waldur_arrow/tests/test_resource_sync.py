"""Tests for Arrow resource sync functionality."""

from decimal import Decimal
from unittest import mock

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.waldur_arrow import tasks
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture


class AggregateSubscriptionsTest(TestCase):
    """Tests for _aggregate_subscriptions_for_resources function."""

    def test_aggregates_iaas_subscriptions(self):
        export_data = {
            "headers": [
                "Classification",
                "Vendor Subscription ID",
                "Friendly Name",
                "Customer Total Price",
                "Total Wholesale Price",
                "End User Company Name",
                "Report Period",
                "Description",
                "Vendor Name",
                "Offer Name",
            ],
            "values": [
                [
                    "IAAS",
                    "sub-001",
                    "Azure Sub 1",
                    "100.00",
                    "80.00",
                    "Customer A",
                    "2024-12",
                    "Usage Azure Sub 1",
                    "Microsoft",
                    "Usage",
                ],
                [
                    "IAAS",
                    "sub-001",
                    "Azure Sub 1",
                    "50.00",
                    "40.00",
                    "Customer A",
                    "2025-01",
                    "Usage Azure Sub 1",
                    "Microsoft",
                    "Usage",
                ],
                [
                    "SAAS",  # Should be filtered out
                    "sub-002",
                    "Office 365",
                    "200.00",
                    "160.00",
                    "Customer A",
                    "2024-12",
                    "Office license",
                    "Microsoft",
                    "License",
                ],
            ],
        }

        subscriptions, customers = tasks._aggregate_subscriptions_for_resources(
            export_data
        )

        self.assertEqual(len(subscriptions), 1)
        self.assertIn("sub-001", subscriptions)
        self.assertEqual(subscriptions["sub-001"]["name"], "Azure Sub 1")
        self.assertEqual(subscriptions["sub-001"]["customer"], "Customer A")
        self.assertEqual(subscriptions["sub-001"]["sell_total"], Decimal("150.00"))
        self.assertEqual(subscriptions["sub-001"]["buy_total"], Decimal("120.00"))
        self.assertEqual(len(subscriptions["sub-001"]["periods"]), 2)

    def test_handles_empty_export(self):
        export_data = {"headers": [], "values": []}

        subscriptions, customers = tasks._aggregate_subscriptions_for_resources(
            export_data
        )

        self.assertEqual(len(subscriptions), 0)
        self.assertEqual(len(customers), 0)

    def test_skips_rows_without_subscription_id(self):
        export_data = {
            "headers": [
                "Classification",
                "Vendor Subscription ID",
                "Friendly Name",
                "Customer Total Price",
            ],
            "values": [
                ["IAAS", "", "No Sub ID", "100.00"],
                ["IAAS", None, "Null Sub ID", "100.00"],
                ["IAAS", "sub-001", "Valid", "100.00"],
            ],
        }

        subscriptions, customers = tasks._aggregate_subscriptions_for_resources(
            export_data
        )

        self.assertEqual(len(subscriptions), 1)
        self.assertIn("sub-001", subscriptions)

    def test_extracts_customer_details_when_requested(self):
        export_data = {
            "headers": [
                "Classification",
                "Vendor Subscription ID",
                "Friendly Name",
                "Customer Total Price",
                "End User Company Name",
                "End User Company ID",
                "End User E-mail",
                "End User City",
                "End User Country Code",
            ],
            "values": [
                [
                    "IAAS",
                    "sub-001",
                    "Azure Sub",
                    "100.00",
                    "Test Company",
                    "COMP123",
                    "test@example.com",
                    "Tallinn",
                    "EE",
                ],
            ],
        }

        subscriptions, customers = tasks._aggregate_subscriptions_for_resources(
            export_data, include_customer_details=True
        )

        self.assertEqual(len(customers), 1)
        self.assertIn("Test Company", customers)
        self.assertEqual(customers["Test Company"]["arrow_id"], "COMP123")
        self.assertEqual(customers["Test Company"]["email"], "test@example.com")
        self.assertEqual(customers["Test Company"]["city"], "Tallinn")
        self.assertEqual(customers["Test Company"]["country"], "EE")
        self.assertIn("sub-001", customers["Test Company"]["subscriptions"])


class SyncResourceFromSubscriptionTest(TestCase):
    """Tests for _sync_resource_from_subscription function."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.project = structure_factories.ProjectFactory()

    def test_creates_new_resource(self):
        info = {
            "name": "Test Azure Sub",
            "customer": "Test Customer",
            "vendor": "Microsoft",
            "sell_total": Decimal("150.00"),
            "buy_total": Decimal("120.00"),
            "periods": {
                "2024-12": {
                    "sell": Decimal("100.00"),
                    "buy": Decimal("80.00"),
                    "items": [
                        {
                            "description": "Usage",
                            "offer": "Usage",
                            "sell": "100.00",
                            "buy": "80.00",
                        }
                    ],
                },
            },
        }

        created = tasks._sync_resource_from_subscription(
            sub_id="test-sub-123",
            info=info,
            offering=self.offering,
            project=self.project,
        )

        self.assertTrue(created)

        resource = marketplace_models.Resource.objects.get(backend_id="test-sub-123")
        self.assertEqual(resource.name, "Test Azure Sub")
        self.assertEqual(resource.offering, self.offering)
        self.assertEqual(resource.project, self.project)
        self.assertIsNotNone(resource.report)
        # Report has 2 sections: Billing Summary + 1 period
        self.assertEqual(len(resource.report), 2)
        self.assertEqual(resource.report[0]["header"], "Billing Summary")
        self.assertEqual(resource.report[1]["header"], "Billing Period: 2024-12")
        self.assertEqual(resource.current_usages["cloud_cost"], "150.00")

    def test_updates_existing_resource(self):
        # Create existing resource
        existing = marketplace_models.Resource.objects.create(
            name="Old Name",
            offering=self.offering,
            project=self.project,
            backend_id="existing-sub-456",
            state=marketplace_models.Resource.States.OK,
        )

        info = {
            "name": "New Name",
            "customer": "Test Customer",
            "vendor": "Microsoft",
            "sell_total": Decimal("200.00"),
            "buy_total": Decimal("160.00"),
            "periods": {
                "2024-12": {
                    "sell": Decimal("200.00"),
                    "buy": Decimal("160.00"),
                    "items": [],
                },
            },
        }

        created = tasks._sync_resource_from_subscription(
            sub_id="existing-sub-456",
            info=info,
            offering=None,
            project=None,
        )

        self.assertFalse(created)

        existing.refresh_from_db()
        self.assertEqual(existing.name, "New Name")
        self.assertEqual(existing.current_usages["cloud_cost"], "200.00")

    def test_returns_false_without_offering_project_for_new_resource(self):
        info = {
            "name": "Test",
            "customer": "Test",
            "vendor": "Microsoft",
            "sell_total": Decimal("100.00"),
            "buy_total": Decimal("80.00"),
            "periods": {},
        }

        created = tasks._sync_resource_from_subscription(
            sub_id="new-sub-789",
            info=info,
            offering=None,
            project=None,
        )

        self.assertFalse(created)
        self.assertFalse(
            marketplace_models.Resource.objects.filter(
                backend_id="new-sub-789"
            ).exists()
        )


class SyncArrowResourcesTaskTest(TestCase):
    """Tests for sync_arrow_resources task."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.offering = marketplace_factories.OfferingFactory()
        self.project = structure_factories.ProjectFactory()

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_sync_creates_resources(self, mock_export):
        self.fixture.arrow_settings

        mock_export.return_value = {
            "headers": [
                "Classification",
                "Vendor Subscription ID",
                "Friendly Name",
                "Customer Total Price",
                "Total Wholesale Price",
                "End User Company Name",
                "Report Period",
                "Description",
                "Vendor Name",
                "Offer Name",
            ],
            "values": [
                [
                    "IAAS",
                    "sync-test-001",
                    "Test Sub 1",
                    "100.00",
                    "80.00",
                    "Customer",
                    "2024-12",
                    "Usage",
                    "Microsoft",
                    "Usage",
                ],
                [
                    "IAAS",
                    "sync-test-002",
                    "Test Sub 2",
                    "200.00",
                    "160.00",
                    "Customer",
                    "2024-12",
                    "Usage",
                    "Microsoft",
                    "Usage",
                ],
            ],
        }

        result = tasks.sync_arrow_resources(
            period_from="2024-12",
            period_to="2025-01",
            offering_uuid=str(self.offering.uuid),
            project_uuid=str(self.project.uuid),
        )

        self.assertEqual(result["synced"], 2)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["updated"], 0)

        self.assertTrue(
            marketplace_models.Resource.objects.filter(
                backend_id="sync-test-001"
            ).exists()
        )
        self.assertTrue(
            marketplace_models.Resource.objects.filter(
                backend_id="sync-test-002"
            ).exists()
        )

    def test_sync_returns_error_without_settings(self):
        # No settings created

        result = tasks.sync_arrow_resources(
            period_from="2024-12",
            period_to="2025-01",
        )

        self.assertIn("error", result)
