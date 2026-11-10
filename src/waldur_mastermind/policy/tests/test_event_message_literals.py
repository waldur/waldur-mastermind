"""Resource names show in policy event messages as written."""

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.tests import factories

TEXT = "AA{{BB}}CC {nosuchkey} {"


@override_settings(task_always_eager=True)
@freeze_time("2024-09-01")
class TerminateResourcesMessageTest(test.APITestCase):
    def test_resource_name_shows_as_written(self):
        fixture = marketplace_fixtures.MarketplaceFixture()
        policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=fixture.project,
            created_by=fixture.user,
            actions="terminate_resources",
        )
        invoice = invoices_factories.InvoiceFactory(
            customer=fixture.customer, month=9, year=2024, tax_percent=0
        )
        resource = fixture.resource
        resource.name = TEXT
        resource.state = ResourceStates.OK
        resource.save()
        resource.offering.type = OPENSTACK_INSTANCE_OFFERING
        resource.offering.save()

        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=fixture.project,
            quantity=1,
            unit_price=policy.limit_cost + 1,
        )

        event = Event.objects.filter(event_type=EventType.TERMINATE_RESOURCES).latest(
            "created"
        )
        self.assertEqual(
            event.message,
            "Cost policy has been triggered and termination order has been "
            f"created. Resource: {resource}.",
        )
        self.assertIn(TEXT, event.message)
