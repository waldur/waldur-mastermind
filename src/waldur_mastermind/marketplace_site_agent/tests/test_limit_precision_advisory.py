"""What a provider is told before they let a component limit carry a fraction.

The site agent is the one plugin that cannot answer this from its offering type:
that single type fronts every agent, and the backends behind it disagree about
whether a limit can hold a fraction. So it declares no cap and advises instead,
per offering, from the backend its agent actually reports.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent.tests import factories


class LimitPrecisionAdvisoryTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def _report_backend(self, *backend_types):
        """Register an agent for the offering reporting these backends."""
        identity = factories.AgentIdentityFactory(offering=self.offering)
        service = factories.AgentServiceFactory(identity=identity)
        for backend_type in backend_types:
            factories.AgentProcessorFactory(service=service, backend_type=backend_type)

    def _advisory(self):
        return manager.get_limit_precision_advisory(self.offering)

    def test_a_backend_that_stores_whole_numbers_is_named(self):
        self._report_backend("slurm")
        advisory = self._advisory()
        self.assertIsNotNone(advisory)
        self.assertIn("slurm", str(advisory))
        self.assertIn("whole numbers", str(advisory))

    def test_the_reported_case_is_not_what_decides(self):
        """The model's own help text says "SLURM"; the agent enum says "slurm"."""
        self._report_backend("SLURM")
        self.assertIsNotNone(self._advisory())

    def test_a_backend_that_holds_fractions_says_nothing(self):
        # litellm overrides the agent's base int() cast precisely because
        # truncating a budget is wrong for it, so warning here would be noise.
        self._report_backend("litellm")
        self.assertIsNone(self._advisory())

    def test_an_offering_with_no_agent_says_nothing(self):
        self.assertIsNone(self._advisory())

    def test_only_the_truncating_backend_is_named(self):
        self._report_backend("litellm", "slurm")
        advisory = str(self._advisory())
        self.assertIn("slurm", advisory)
        self.assertNotIn("litellm", advisory)

    def test_several_truncating_backends_are_all_named(self):
        self._report_backend("slurm", "harbor")
        advisory = str(self._advisory())
        self.assertIn("slurm", advisory)
        self.assertIn("harbor", advisory)

    def test_a_plugin_that_declares_no_advisory_says_nothing(self):
        """Every other offering type answers through max_limit_decimal_places,
        so the hook is absent and the accessor must not reach for it."""
        offering = marketplace_factories.OfferingFactory(type=enums.BASIC_OFFERING)
        self.assertIsNone(manager.get_limit_precision_advisory(offering))


class LimitPrecisionAdvisoryApiTest(test.APITestCase):
    """The advisory is composed server-side and handed over finished, so the
    frontend never has to hold the list of backends that truncate."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.client.force_authenticate(self.fixture.staff)

    def _get(self):
        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_the_offering_carries_the_advisory(self):
        identity = factories.AgentIdentityFactory(offering=self.offering)
        service = factories.AgentServiceFactory(identity=identity)
        factories.AgentProcessorFactory(service=service, backend_type="slurm")

        advisory = self._get()["limit_precision_advisory"]
        self.assertIsNotNone(advisory)
        self.assertIn("slurm", advisory)

    def test_the_field_is_null_when_there_is_nothing_to_say(self):
        self.assertIsNone(self._get()["limit_precision_advisory"])

    def test_a_list_page_does_not_pay_a_query_per_offering(self):
        """The plugin answers this with a query, and this serializer serves the
        offering list as well as the detail, so an ungated field would cost one
        per row. The component editor reads offerings one at a time."""
        for _ in range(5):
            offering = marketplace_factories.OfferingFactory(
                customer=self.fixture.offering_customer,
                type=enums.SITE_AGENT_OFFERING,
            )
            identity = factories.AgentIdentityFactory(offering=offering)
            service = factories.AgentServiceFactory(identity=identity)
            factories.AgentProcessorFactory(service=service, backend_type="slurm")

        url = marketplace_factories.OfferingFactory.get_list_url()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 5)

        agent_queries = [
            q
            for q in ctx.captured_queries
            if "marketplace_site_agent_agentprocessor" in q["sql"]
        ]
        self.assertEqual(
            agent_queries,
            [],
            "the offering list is asking the site agent about every row",
        )

    def test_the_public_offering_endpoint_does_not_carry_it(self):
        """It names the provider's backend and exists for the component
        editor; a consumer browsing the marketplace has nothing to configure
        with it."""
        identity = factories.AgentIdentityFactory(offering=self.offering)
        service = factories.AgentServiceFactory(identity=identity)
        factories.AgentProcessorFactory(service=service, backend_type="slurm")
        self.offering.state = OfferingStates.ACTIVE
        self.offering.shared = True
        self.offering.save()

        url = marketplace_factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("limit_precision_advisory", response.data)

    def test_a_list_request_may_still_ask_for_it_by_name(self):
        """Naming the field drops every other one, so the caller has asked for
        the cost knowingly -- the same escape hatch the expensive annotations
        on this viewset offer."""
        identity = factories.AgentIdentityFactory(offering=self.offering)
        service = factories.AgentServiceFactory(identity=identity)
        factories.AgentProcessorFactory(service=service, backend_type="slurm")

        url = marketplace_factories.OfferingFactory.get_list_url()
        response = self.client.get(
            url, {"field": "limit_precision_advisory", "uuid": self.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIn("slurm", response.data[0]["limit_precision_advisory"])
