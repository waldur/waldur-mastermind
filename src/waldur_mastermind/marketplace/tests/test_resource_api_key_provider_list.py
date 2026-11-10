"""A service provider's API keys across its offerings: who sees them, how they
filter and sort, what a key's usage is estimated to cost, and the key counts on
the provider resource list."""

from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions import utils as permission_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    GlobalRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import api_key_cost, models
from waldur_mastermind.marketplace.enums import SLURM_OFFERING
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.tests.test_resource_api_key_management import (
    LAST_MONTH,
    THIS_MONTH,
    ApiKeyManagementTestBase,
    detail_url,
    list_url,
)

States = models.ResourceApiKey.States


def make_key(resource, client_id, state=States.OK, **kwargs):
    return models.ResourceApiKey.objects.create(
        resource=resource, client_id=client_id, state=state, **kwargs
    )


def manage_keys(*offerings):
    """Turn on key management, under which keys carry usage and a cost."""
    for offering in offerings:
        offering.plugin_options = {"enable_api_key_provisioning": True}
        offering.save()


def uuids(response):
    return {row["uuid"] for row in response.data}


class ProviderVisibilityTest(test.APITestCase):
    """provider_uuid lists a provider's keys as the provider resource view does."""

    def setUp(self):
        self.provider = MarketplaceFixture()
        self.key = make_key(self.provider.resource, "cid-1")
        # A second resource of the same provider, in another consumer's project.
        other_project = structure_factories.ProjectFactory()
        self.other_resource = factories.ResourceFactory(
            offering=self.provider.offering,
            plan=self.provider.plan,
            project=other_project,
        )
        self.other_key = make_key(self.other_resource, "cid-2")
        self.elsewhere = MarketplaceFixture()
        self.foreign_key = make_key(self.elsewhere.resource, "cid-3")

    def list_for(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(
            list_url(),
            {"provider_uuid": self.provider.offering_customer.uuid.hex, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response

    def test_every_provider_side_caller_sees_all_the_providers_keys(self):
        service_provider_manager = structure_factories.UserFactory()
        self.provider.service_provider.add_user(
            service_provider_manager, ServiceProviderRole.MANAGER
        )
        global_reader = structure_factories.UserFactory()
        role = GlobalRole.get("GLOBAL.SUPPORT")
        role.add_permission(PermissionEnum.LIST_RESOURCES)
        permission_utils.grant_global_role(global_reader, role)
        callers = (
            ("staff", self.provider.staff),
            ("support", structure_factories.UserFactory(is_support=True)),
            ("global grant that sees all resources", global_reader),
            ("provider owner", self.provider.offering_owner),
            ("service provider manager", service_provider_manager),
            ("offering manager", self.provider.offering_manager),
        )
        for label, user in callers:
            with self.subTest(label):
                self.assertEqual(
                    uuids(self.list_for(user)),
                    {self.key.uuid.hex, self.other_key.uuid.hex},
                )

    def test_a_consumer_without_a_provider_role_gets_nothing(self):
        # The owner of the consuming organization sees this key in an unfiltered
        # list, but not as the provider's.
        self.client.force_authenticate(self.provider.owner)
        self.assertIn(self.key.uuid.hex, uuids(self.client.get(list_url())))
        self.assertEqual(uuids(self.list_for(self.provider.owner)), set())

    def test_a_provider_who_is_also_a_consumer_gets_only_the_providers_keys(self):
        user = self.provider.offering_owner
        # The same person consumes another provider's offering.
        self.elsewhere.customer.add_user(user, CustomerRole.OWNER)
        self.client.force_authenticate(user)
        self.assertIn(self.foreign_key.uuid.hex, uuids(self.client.get(list_url())))

        self.assertEqual(
            uuids(self.list_for(user)), {self.key.uuid.hex, self.other_key.uuid.hex}
        )

    def test_another_providers_staff_gets_nothing(self):
        self.assertEqual(uuids(self.list_for(self.elsewhere.offering_owner)), set())

    def test_the_list_never_carries_a_key_value(self):
        response = self.list_for(self.provider.offering_owner)
        for row in response.data:
            self.assertNotIn("api_key", row)
            self.assertNotIn("key_ciphertext", row)

    def test_rows_carry_their_context(self):
        response = self.list_for(self.provider.offering_owner)
        row = next(r for r in response.data if r["uuid"] == self.key.uuid.hex)
        resource = self.provider.resource
        self.assertEqual(row["resource_name"], resource.name)
        self.assertEqual(row["offering_uuid"], resource.offering.uuid)
        self.assertEqual(row["offering_name"], resource.offering.name)
        self.assertEqual(row["customer_uuid"], resource.project.customer.uuid)
        self.assertEqual(row["customer_name"], resource.project.customer.name)
        self.assertEqual(row["project_uuid"], resource.project.uuid)
        self.assertEqual(row["project_name"], resource.project.name)
        self.assertEqual(row["resource_plan_uuid"], resource.plan.uuid)
        self.assertEqual(row["resource_spending_limits"], {})

    def test_rows_carry_the_resource_limits_that_cap_the_key(self):
        resource = self.provider.resource
        resource.spending_limits = {"token_cost": {"month": 100}}
        resource.save(update_fields=["spending_limits"])
        response = self.list_for(self.provider.offering_owner)
        row = next(r for r in response.data if r["uuid"] == self.key.uuid.hex)
        self.assertEqual(
            row["resource_spending_limits"], {"token_cost": {"month": 100.0}}
        )

    def test_a_resource_without_a_plan_has_no_plan_uuid(self):
        self.other_resource.plan = None
        self.other_resource.save(update_fields=["plan"])
        response = self.list_for(self.provider.offering_owner)
        row = next(r for r in response.data if r["uuid"] == self.other_key.uuid.hex)
        self.assertIsNone(row["resource_plan_uuid"])

    def test_more_resources_cost_no_more_queries(self):
        def queries():
            with CaptureQueriesContext(connection) as context:
                self.list_for(self.provider.offering_owner)
            return len(context.captured_queries)

        # The first request of a test fills caches the later ones read.
        queries()
        before = queries()
        for index in range(3):
            resource = factories.ResourceFactory(
                offering=self.provider.offering,
                plan=factories.PlanFactory(offering=self.provider.offering),
                project=structure_factories.ProjectFactory(),
            )
            make_key(resource, f"more-{index}")
        self.assertEqual(queries(), before)


class ProviderFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.key = make_key(self.resource, "cid-1")
        self.paused = make_key(
            self.resource, "cid-2", state=States.PAUSED, paused_by_limit=True
        )
        self.deleted = make_key(self.resource, "cid-3", state=States.DELETED)
        self.other_project = structure_factories.ProjectFactory()
        self.other_resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.other_project,
        )
        self.other_key = make_key(self.other_resource, "cid-4")
        self.client.force_authenticate(self.fixture.offering_owner)

    def list(self, **params):
        response = self.client.get(
            list_url(),
            {"provider_uuid": self.fixture.offering_customer.uuid.hex, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return uuids(response)

    def test_by_consumer_organization(self):
        self.assertEqual(
            self.list(customer_uuid=self.other_project.customer.uuid.hex),
            {self.other_key.uuid.hex},
        )

    def test_by_project(self):
        self.assertEqual(
            self.list(project_uuid=self.fixture.project.uuid.hex),
            {self.key.uuid.hex, self.paused.uuid.hex},
        )

    def test_paused_by_limit_counts_only_keys_under_key_management(self):
        # The list shows paused_by_limit as null once the offering stops
        # managing keys, so the filter must not call such a key paused.
        manage_keys(self.fixture.offering)
        unmanaged = factories.OfferingFactory(customer=self.fixture.offering_customer)
        stale = make_key(
            factories.ResourceFactory(offering=unmanaged, project=self.fixture.project),
            "cid-5",
            state=States.PAUSED,
            paused_by_limit=True,
        )
        self.assertEqual(self.list(paused_by_limit="true"), {self.paused.uuid.hex})
        self.assertIn(stale.uuid.hex, self.list(paused_by_limit="false"))

    def test_by_paused_by_limit(self):
        manage_keys(self.fixture.offering)
        self.assertEqual(self.list(paused_by_limit="true"), {self.paused.uuid.hex})
        self.assertEqual(
            self.list(paused_by_limit="false"),
            {self.key.uuid.hex, self.other_key.uuid.hex},
        )

    def test_deleted_keys_only_when_asked_for(self):
        self.assertNotIn(self.deleted.uuid.hex, self.list())
        self.assertEqual(self.list(state=States.DELETED), {self.deleted.uuid.hex})


class EstimatedCostTest(ApiKeyManagementTestBase):
    """A key's usage priced at its resource plan's component prices."""

    def setUp(self):
        super().setUp()
        self.tokens = self.offering.components.get(type="tokens")
        factories.PlanComponentFactory(
            plan=self.resource.plan, component=self.tokens, price=Decimal("0.002")
        )
        # Offered, but not priced in the resource's plan.
        factories.OfferingComponentFactory(offering=self.offering, type="images")

    def report(self, key, usages, billing_period=None):
        data = {"usages": usages}
        if billing_period:
            data["billing_period"] = billing_period.isoformat()
        response, _ = self.post(
            self.fixture.offering_owner, detail_url(key, "report-usage"), data
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def test_usage_times_the_plan_price(self):
        response = self.report(self.key, {"tokens": 1500})
        self.key.refresh_from_db()
        self.assertEqual(self.key.estimated_cost, Decimal("3"))
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("3"))

    def test_a_component_without_a_plan_price_counts_as_zero(self):
        self.report(self.key, {"tokens": 1000, "images": 40})
        self.key.refresh_from_db()
        self.assertEqual(self.key.estimated_cost, Decimal("2"))

    def test_every_report_recomputes_it(self):
        self.report(self.key, {"tokens": 1000})
        self.report(self.key, {"tokens": 2500})
        self.key.refresh_from_db()
        self.assertEqual(self.key.estimated_cost, Decimal("5"))

    def test_a_price_change_shows_at_the_next_report(self):
        self.report(self.key, {"tokens": 1000})
        self.resource.plan.components.filter(component=self.tokens).update(
            price=Decimal("0.01")
        )
        self.report(self.key, {"tokens": 1000})
        self.key.refresh_from_db()
        self.assertEqual(self.key.estimated_cost, Decimal("10"))

    def test_a_resource_without_a_plan_costs_nothing(self):
        self.resource.plan = None
        self.resource.save()
        self.report(self.key, {"tokens": 1000})
        self.key.refresh_from_db()
        self.assertEqual(self.key.estimated_cost, Decimal("0"))

    def test_last_months_figure_reads_as_zero_this_month(self):
        self.report(self.key, {"tokens": 1000}, LAST_MONTH)
        self.key.refresh_from_db()
        self.assertEqual(self.key.estimated_cost, Decimal("2"))
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(detail_url(self.key))
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("0"))

    def test_usage_is_converted_as_for_invoices(self):
        # A SLURM offering reports GPU minutes and prices GPU hours.
        self.offering.type = SLURM_OFFERING
        self.offering.save()
        self.tokens.type = "gpu"
        self.tokens.save()
        self.key.current_usages = {"gpu": 150}
        self.key.save()
        self.key.refresh_from_db()
        # 150 minutes bill as 3 hours (rounded up), at 0.002 an hour.
        self.assertEqual(api_key_cost.estimate(self.key), Decimal("0.006"))

    def test_estimate_matches_the_stored_figure(self):
        self.report(self.key, {"tokens": 700})
        self.key.refresh_from_db()
        self.assertEqual(api_key_cost.estimate(self.key), self.key.estimated_cost)


class EstimatedCostOrderingTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.offering = self.fixture.offering
        second_offering = factories.OfferingFactory(
            customer=self.fixture.offering_customer
        )
        second_resource = factories.ResourceFactory(
            offering=second_offering, project=self.fixture.project
        )
        manage_keys(self.offering, second_offering)
        self.cheap = make_key(
            self.fixture.resource,
            "cid-1",
            estimated_cost=Decimal("1.5"),
            usage_period=THIS_MONTH,
        )
        self.dear = make_key(
            second_resource,
            "cid-2",
            estimated_cost=Decimal("40"),
            usage_period=THIS_MONTH,
        )
        # Dearer still, but last month's: nothing spent this month yet.
        self.stale = make_key(
            self.fixture.resource,
            "cid-3",
            estimated_cost=Decimal("900"),
            usage_period=LAST_MONTH,
        )
        self.client.force_authenticate(self.fixture.offering_owner)

    def ordered(self, ordering):
        response = self.client.get(
            list_url(),
            {"provider_uuid": self.fixture.offering_customer.uuid.hex, "o": ordering},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["uuid"] for row in response.data]

    def test_descending_across_offerings(self):
        self.assertEqual(
            self.ordered("-estimated_cost"),
            [self.dear.uuid.hex, self.cheap.uuid.hex, self.stale.uuid.hex],
        )

    def test_ascending(self):
        self.assertEqual(
            self.ordered("estimated_cost"),
            [self.stale.uuid.hex, self.cheap.uuid.hex, self.dear.uuid.hex],
        )

    def test_a_key_whose_offering_stopped_managing_keys_ranks_as_zero(self):
        # Its cost reads as null in the list, so it must not top the ranking.
        unmanaged = factories.OfferingFactory(customer=self.fixture.offering_customer)
        hidden = make_key(
            factories.ResourceFactory(offering=unmanaged, project=self.fixture.project),
            "cid-4",
            estimated_cost=Decimal("5000"),
            usage_period=THIS_MONTH,
        )
        ordered = self.ordered("-estimated_cost")
        self.assertEqual(ordered[:2], [self.dear.uuid.hex, self.cheap.uuid.hex])
        self.assertIn(hidden.uuid.hex, ordered[2:])
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(detail_url(hidden))
        self.assertIsNone(response.data["estimated_cost"])

    def test_other_orderings_are_accepted(self):
        for ordering in ("created", "-issued_at", "state"):
            with self.subTest(ordering):
                self.assertEqual(len(self.ordered(ordering)), 3)


class ProviderUsageTotalsTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        second_offering = factories.OfferingFactory(
            customer=self.fixture.offering_customer
        )
        second_resource = factories.ResourceFactory(
            offering=second_offering, project=self.fixture.project
        )
        manage_keys(self.fixture.offering, second_offering)
        make_key(
            self.fixture.resource,
            "cid-1",
            current_usages={"tokens": 100},
            usage_period=THIS_MONTH,
            estimated_cost=Decimal("2.5"),
        )
        make_key(
            second_resource,
            "cid-2",
            current_usages={"storage": 3},
            usage_period=THIS_MONTH,
            estimated_cost=Decimal("4"),
            state=States.DELETED,
        )
        make_key(
            second_resource,
            "cid-3",
            current_usages={"storage": 50},
            usage_period=LAST_MONTH,
            estimated_cost=Decimal("100"),
        )
        # Another provider's key is never summed.
        make_key(
            MarketplaceFixture().resource,
            "cid-4",
            current_usages={"tokens": 7},
            usage_period=THIS_MONTH,
            estimated_cost=Decimal("7"),
        )
        self.client.force_authenticate(self.fixture.offering_owner)

    def totals(self, **params):
        return self.client.get(list_url("usage-totals"), params)

    def test_a_providers_totals_span_its_offerings(self):
        response = self.totals(provider_uuid=self.fixture.offering_customer.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["usages"], {"tokens": 100, "storage": 3})
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("6.5"))

    def test_the_list_filters_narrow_the_totals(self):
        # The provider page's summary sends its filters along.
        response = self.totals(
            provider_uuid=self.fixture.offering_customer.uuid.hex,
            state=States.DELETED,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["usages"], {"storage": 3})
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("4"))

    def test_an_offerings_totals(self):
        response = self.totals(offering_uuid=self.fixture.offering.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["usages"], {"tokens": 100})
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("2.5"))

    def test_a_resources_totals_carry_the_estimate_too(self):
        response = self.totals(resource_uuid=self.fixture.resource.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("2.5"))

    def test_keys_of_an_offering_without_key_management_add_no_cost(self):
        unmanaged = factories.OfferingFactory(customer=self.fixture.offering_customer)
        make_key(
            factories.ResourceFactory(offering=unmanaged, project=self.fixture.project),
            "cid-5",
            current_usages={"tokens": 1},
            usage_period=THIS_MONTH,
            estimated_cost=Decimal("1000"),
        )
        response = self.totals(provider_uuid=self.fixture.offering_customer.uuid.hex)
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("6.5"))

    def test_a_scope_is_required(self):
        response = self.totals(state=States.OK)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_consumer_gets_no_provider_totals(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.totals(provider_uuid=self.fixture.offering_customer.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["usages"], {})
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("0"))


class ResourceApiKeyCountsTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        for index, state in enumerate(
            (States.OK, States.OK, States.PAUSED, States.ERRED, States.DELETED)
        ):
            make_key(self.resource, f"cid-{index}", state=state)
        self.client.force_authenticate(self.fixture.offering_owner)

    def provider_list(self):
        response = self.client.get(
            reverse("marketplace-provider-resource-list"),
            {"field": ["uuid", "api_key_counts", "has_api_keys"]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"]: row for row in response.data}

    def test_counts_per_state_without_deleted_keys(self):
        row = self.provider_list()[self.resource.uuid.hex]
        self.assertEqual(row["api_key_counts"], {"OK": 2, "Paused": 1, "Erred": 1})
        self.assertTrue(row["has_api_keys"])

    def test_a_resource_without_keys_has_no_counts(self):
        bare = factories.ResourceFactory(
            offering=self.fixture.offering, project=self.fixture.project
        )
        self.assertEqual(self.provider_list()[bare.uuid.hex]["api_key_counts"], {})

    def test_one_query_for_the_page(self):
        def key_queries():
            with CaptureQueriesContext(connection) as context:
                self.provider_list()
            return sum(
                '"marketplace_resourceapikey"' in query["sql"]
                for query in context.captured_queries
            )

        before = key_queries()
        for index in range(3):
            resource = factories.ResourceFactory(
                offering=self.fixture.offering, project=self.fixture.project
            )
            make_key(resource, f"more-{index}")
        self.assertEqual(key_queries(), before)

    def test_a_single_resource_counts_too(self):
        response = self.client.get(
            reverse(
                "marketplace-provider-resource-detail",
                kwargs={"uuid": self.resource.uuid.hex},
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["api_key_counts"], {"OK": 2, "Paused": 1, "Erred": 1}
        )
