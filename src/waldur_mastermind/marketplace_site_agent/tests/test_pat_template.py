"""The site agent's personal access token template.

Three things are checked: who is offered the template and where its token can
be bound; that a token carrying exactly the template's scopes can do the agent's
work there and nothing outside it; and that every action the agent calls is
checked against a scope the template carries.
"""

import textwrap
from datetime import timedelta

from ddt import data, ddt
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import ReauthenticationAction
from waldur_core.core.models import User
from waldur_core.core.tests.helpers import reauthenticated
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import TYPE_MAP, PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.permissions.tests.factories import RoleFactory
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.permissions.utils import grant_global_role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import views as marketplace_views
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent import models
from waldur_mastermind.marketplace_site_agent.pat_templates import (
    REQUIRED_SCOPES,
    ROLE_PERMISSIONS,
    SITE_AGENT_TEMPLATE_KEY,
)
from waldur_mastermind.marketplace_site_agent.tests import factories
from waldur_mastermind.policy import models as policy_models
from waldur_mastermind.policy import views as policy_views
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory

TEMPLATES_URL = "/api/personal-access-tokens/templates/"
TARGETS_URL = "/api/personal-access-tokens/template_targets/"
REQUIRED = [scope.value for scope in REQUIRED_SCOPES]


def grant_template_permissions():
    # Test roles start bare: permissions.yaml is not loaded here. The role
    # permissions (ORDER.LIST, OFFERING.UPDATE) stay on the role: the agent's
    # reads follow them, the token does not carry them.
    for role in (CustomerRole.OWNER, OfferingRole.MANAGER):
        for permission in (*REQUIRED_SCOPES, *ROLE_PERMISSIONS):
            role.add_permission(permission)


class SiteAgentFixtureMixin:
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = marketplace_enums.SITE_AGENT_OFFERING
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        grant_template_permissions()

    def user(self, user_role):
        if user_role == "owner_as_manager":
            # An owner adds themselves as the offering's manager to get a
            # site-agent token.
            owner = self.fixture.offering_owner
            self.offering.add_user(owner, OfferingRole.MANAGER)
            return owner
        return getattr(self.fixture, user_role)

    def allow_tokens(self, user):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])

    def site_agent_template(self, user):
        self.allow_tokens(user)
        self.client.force_authenticate(user)
        response = self.client.get(TEMPLATES_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return next(
            (t for t in response.data if t["key"] == SITE_AGENT_TEMPLATE_KEY), None
        )

    def target(self, user):
        """The target for this offering, as a client gets it."""
        self.allow_tokens(user)
        self.client.force_authenticate(user)
        response = self.client.get(
            TARGETS_URL,
            {"template": SITE_AGENT_TEMPLATE_KEY, "uuid": self.offering.uuid.hex},
        )
        self.client.force_authenticate(None)
        (target,) = response.data
        return target

    def template_bindings(self, user):
        """The objects the target's bindings name, as a client sends them."""
        return [
            apps.get_model(*TYPE_MAP[binding["type"]]).objects.get(uuid=binding["uuid"])
            for binding in self.target(user)["bindings"]
        ]

    def targets(self, user, **params):
        self.allow_tokens(user)
        self.client.force_authenticate(user)
        response = self.client.get(
            TARGETS_URL, {"template": SITE_AGENT_TEMPLATE_KEY, **params}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {(t["type"], str(t["uuid"]).replace("-", "")) for t in response.data}


@ddt
@override_config(PAT_ENABLED=True)
class SiteAgentTemplateVisibilityTest(SiteAgentFixtureMixin, test.APITestCase):
    @data("offering_manager", "owner_as_manager", "offering_owner")
    def test_offered_to_who_holds_the_scopes_on_the_offering_or_above(self, user_role):
        template = self.site_agent_template(self.user(user_role))
        self.assertIsNotNone(template)
        self.assertEqual(template["required_scopes"], REQUIRED)
        self.assertEqual(template["binding_types"], ["offering"])

    @data("member", "admin", "manager", "owner", "offering_admin")
    def test_hidden_from_who_does_not_hold_the_scopes(self, user_role):
        # "owner" owns the consumer organization, which sells nothing.
        self.assertIsNone(self.site_agent_template(self.user(user_role)))

    def test_staff_without_roles_get_unrestricted_targets(self):
        # A staff user's token without STAFF.ACCESS acts as the user without
        # staff, so the token works only with STAFF.ACCESS, unrestricted.
        staff = self.fixture.staff
        self.assertIsNotNone(self.site_agent_template(staff))
        target = self.target(staff)
        self.assertTrue(target["staff_access"])
        self.assertEqual(target["bindings"], [])
        self.assertIn("ssh_keys", target["features"])

    def test_staff_managing_the_offering_get_a_scoped_target(self):
        staff = self.fixture.staff
        self.offering.add_user(staff, OfferingRole.MANAGER)
        target = self.target(staff)
        self.assertFalse(target["staff_access"])
        self.assertEqual(
            [b["uuid"].replace("-", "") for b in target["bindings"]],
            [self.offering.uuid.hex],
        )

    def test_non_staff_never_get_staff_access(self):
        for user in (self.fixture.offering_manager, self.fixture.offering_owner):
            self.assertFalse(self.target(user)["staff_access"])

    def test_hidden_when_the_role_lacks_what_the_agent_reads_by(self):
        # A role with exactly the token's scopes but not ORDER.LIST would give
        # a token that sees no orders.
        user = structure_factories.UserFactory()
        role = RoleFactory(
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering)
        )
        for scope in REQUIRED_SCOPES:
            role.add_permission(scope)
        self.offering.add_user(user, role)
        self.assertIsNone(self.site_agent_template(user))

        for permission in ROLE_PERMISSIONS:
            role.add_permission(permission)
        self.assertIsNotNone(self.site_agent_template(user))

    def test_hidden_when_scopes_come_from_different_offerings(self):
        user = structure_factories.UserFactory()
        other = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            type=marketplace_enums.SITE_AGENT_OFFERING,
        )
        offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
        everything = (*REQUIRED_SCOPES, *ROLE_PERMISSIONS)
        half = len(everything) // 2
        for offering, permissions in (
            (self.offering, everything[:half]),
            (other, everything[half:]),
        ):
            role = RoleFactory(content_type=offering_ct)
            for permission in permissions:
                role.add_permission(permission)
            offering.add_user(user, role)
        self.assertIsNone(self.site_agent_template(user))

    def test_site_wide_grants_make_no_target(self):
        user = structure_factories.UserFactory()
        global_ct = ContentType.objects.get_by_natural_key(*TYPE_MAP["global"])
        role = RoleFactory(content_type=global_ct)
        for permission in (
            *REQUIRED_SCOPES,
            *ROLE_PERMISSIONS,
            PermissionEnum.LIST_OFFERINGS,
        ):
            role.add_permission(permission)
        grant_global_role(user, role)
        self.assertIsNone(self.site_agent_template(user))

    def test_hidden_for_an_offering_site_agents_do_not_serve(self):
        self.offering.type = marketplace_enums.BOOKING_OFFERING
        self.offering.save()
        self.assertIsNone(self.site_agent_template(self.fixture.offering_manager))

    @data("offering_manager", "offering_owner")
    def test_hidden_when_the_organization_is_not_a_service_provider(self, user_role):
        user = self.user(user_role)
        self.fixture.service_provider.delete()
        self.assertIsNone(self.site_agent_template(user))

    def test_hidden_for_an_archived_offering(self):
        self.offering.state = marketplace_models.Offering.States.ARCHIVED
        self.offering.save()
        self.assertIsNone(self.site_agent_template(self.fixture.offering_manager))

    def test_shown_to_a_user_without_the_flag_while_tokens_are_on(self):
        user = self.fixture.offering_manager
        self.assertFalse(user.can_use_personal_access_tokens)
        self.client.force_authenticate(user)
        keys = [t["key"] for t in self.client.get(TEMPLATES_URL).data]
        self.assertIn(SITE_AGENT_TEMPLATE_KEY, keys)

    @override_config(PAT_ENABLED=False)
    def test_shown_to_a_flagged_user_while_tokens_are_off(self):
        self.assertIsNotNone(self.site_agent_template(self.fixture.offering_manager))

    @override_config(PAT_ENABLED=False)
    def test_hidden_from_who_cannot_create_tokens(self):
        user = self.fixture.offering_manager
        self.client.force_authenticate(user)
        user.can_use_personal_access_tokens = False
        user.save(update_fields=["can_use_personal_access_tokens"])
        self.assertEqual(self.client.get(TEMPLATES_URL).data, [])
        self.assertEqual(
            self.client.get(TARGETS_URL, {"template": SITE_AGENT_TEMPLATE_KEY}).data,
            [],
        )

    @override_config(PAT_MAX_LIFETIME_DAYS=30)
    def test_lifetime_is_capped_by_the_site_maximum(self):
        template = self.site_agent_template(self.fixture.offering_manager)
        self.assertEqual(template["default_lifetime_days"], 30)

    def test_target_is_the_offering_and_binds_only_it(self):
        self.assertEqual(
            self.targets(self.fixture.offering_manager),
            {("offering", self.offering.uuid.hex)},
        )
        self.assertEqual(
            self.template_bindings(self.fixture.offering_manager), [self.offering]
        )

    def test_targets_filtered_by_uuid(self):
        self.assertEqual(
            self.targets(self.fixture.offering_manager, uuid=self.offering.uuid.hex),
            {("offering", self.offering.uuid.hex)},
        )
        self.assertEqual(
            self.targets(
                self.fixture.offering_manager,
                uuid=self.offering.customer.uuid.hex,
            ),
            set(),
        )
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(
            TARGETS_URL, {"template": SITE_AGENT_TEMPLATE_KEY, "uuid": "nope"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_targets_filtered_by_name(self):
        self.assertEqual(
            self.targets(self.fixture.offering_manager, name="no such name"), set()
        )

    def test_optional_feature_offered_where_its_scope_is_held(self):
        user = self.fixture.offering_manager
        self.assertNotIn("sync_components", self.target(user)["features"])
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        self.assertIn("sync_components", self.target(user)["features"])

    def test_unknown_template(self):
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(TARGETS_URL, {"template": "nope"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refused_to_a_personal_access_token(self):
        user = self.fixture.offering_manager
        use_pat(self.client, user, *REQUIRED, bindings=[self.offering])
        for url in (TEMPLATES_URL, f"{TARGETS_URL}?template={SITE_AGENT_TEMPLATE_KEY}"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, url)


@ddt
@override_config(PAT_ENABLED=True)
class SiteAgentTokenTest(SiteAgentFixtureMixin, test.APITestCase):
    """A token with exactly the template's scopes, bound where the template
    says, does the agent's work there and nothing else."""

    def use_template_token(self, user_role):
        user = self.user(user_role)
        bindings = self.template_bindings(user)
        use_pat(self.client, user, *REQUIRED, bindings=bindings)
        return user

    @data("offering_manager", "owner_as_manager", "offering_owner")
    def test_finds_the_offerings_service_provider(self, user_role):
        # The agent looks its provider up before anything else and stops when
        # the list is empty.
        self.use_template_token(user_role)
        response = self.client.get(
            "/api/marketplace-service-providers/",
            {"customer_uuid": self.offering.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.data],
            [self.fixture.service_provider.uuid.hex],
        )

    @data("owner_as_manager", "offering_owner")
    def test_cannot_list_the_providers_team(self, user_role):
        # The offering binding reaches the provider's record for the agent's
        # lookup only, not its detail actions: list_users checks the user's
        # roles alone, and the owner could otherwise read the provider's team.
        self.use_template_token(user_role)
        response = self.client.get(
            marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider, "list_users"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("offering_manager", "owner_as_manager", "offering_owner")
    def test_sees_and_updates_the_offerings_resources(self, user_role):
        resource = self.fixture.resource
        resource.state = marketplace_models.Resource.States.ERRED
        resource.save()
        self.use_template_token(user_role)
        response = self.client.get(
            "/api/marketplace-provider-resources/",
            {"offering_uuid": self.offering.uuid.hex},
        )
        self.assertEqual([row["uuid"] for row in response.data], [resource.uuid.hex])
        response = self.client.get(
            marketplace_factories.ResourceFactory.get_provider_resource_url(
                resource, "team"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.client.post(
            marketplace_factories.ResourceFactory.get_provider_resource_url(
                resource, "set_as_ok"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def order(self, state, offering=None, project=None):
        offering = offering or self.offering
        return marketplace_factories.OrderFactory(
            offering=offering,
            project=project or self.fixture.project,
            plan=marketplace_factories.PlanFactory(offering=offering),
            state=state,
        )

    @data("offering_manager", "owner_as_manager", "offering_owner")
    def test_approves_and_finishes_orders(self, user_role):
        self.use_template_token(user_role)
        order = self.order(marketplace_enums.OrderStates.PENDING_PROVIDER)
        response = self.client.post(
            marketplace_factories.OrderFactory.get_url(order, "approve_by_provider")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = self.order(marketplace_enums.OrderStates.EXECUTING)
        response = self.client.post(
            marketplace_factories.OrderFactory.get_url(order, "set_state_done")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("owner_as_manager", "offering_owner")
    def test_cannot_approve_the_organizations_own_purchases(self, user_role):
        # ORDER.APPROVE is also the consumer's approval. An owner's agent token
        # must not approve what the owner's organization buys.
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        other = marketplace_factories.OfferingFactory(
            type=marketplace_enums.SITE_AGENT_OFFERING
        )
        purchase = self.order(
            marketplace_enums.OrderStates.PENDING_CONSUMER,
            offering=other,
            project=self.fixture.offering_project,
        )
        self.use_template_token(user_role)
        response = self.client.post(
            marketplace_factories.OrderFactory.get_url(purchase, "approve_by_consumer")
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.state, marketplace_enums.OrderStates.PENDING_CONSUMER)

    @data("offering_manager", "offering_owner")
    def test_reports_usage(self, user_role):
        component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="agent_cpu",
            billing_type=marketplace_enums.BillingTypes.USAGE,
        )
        plan = marketplace_factories.PlanFactory(offering=self.offering)
        marketplace_factories.PlanComponentFactory(plan=plan, component=component)
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=plan,
            project=self.fixture.project,
            state=marketplace_models.Resource.States.OK,
        )
        self.use_template_token(user_role)
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/",
            {
                "resource": resource.uuid.hex,
                "usages": [{"type": "agent_cpu", "amount": 5}],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("offering_manager", "offering_owner")
    def test_moves_offering_users(self, user_role):
        offering_user = marketplace_factories.OfferingUserFactory(
            offering=self.offering
        )
        offering_user.state = marketplace_enums.OfferingUserStates.CREATION_REQUESTED
        offering_user.save()
        self.use_template_token(user_role)
        response = self.client.post(
            marketplace_factories.OfferingUserFactory.get_url(offering_user)
            + "begin_creating/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("offering_manager", "offering_owner")
    def test_registers_the_agent(self, user_role):
        self.use_template_token(user_role)
        response = self.client.post(
            factories.AgentIdentityFactory.get_list_url(),
            {
                "name": "agent",
                "offering": self.offering.uuid.hex,
                "config_file_path": "/etc/waldur/agent.yaml",
                "config_file_content": textwrap.dedent("""
                    offerings: []
                """),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            models.AgentIdentity.objects.filter(offering=self.offering).exists()
        )

    @data("offering_manager", "offering_owner")
    def test_sets_offering_backend_metadata(self, user_role):
        self.use_template_token(user_role)
        response = self.client.post(
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "set_backend_metadata"
            ),
            {"backend_metadata": {"cluster": "a"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("offering_manager", "owner_as_manager", "offering_owner")
    def test_reads_its_configuration(self, user_role):
        # An agent with configuration_source: waldur loads it at startup
        # (waldur/waldur-site-agent#80).
        self.use_template_token(user_role)
        response = self.client.get(
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "site_agent_config"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("version", response.data)

    @data("offering_manager", "offering_owner")
    def test_cannot_read_its_configuration_without_manage_agent(self, user_role):
        # Bound to its own offering, so the request reaches the permission
        # check: neither MANAGE_AGENT nor UPDATE_INTEGRATION is in the token.
        user = self.user(user_role)
        bindings = self.template_bindings(user)
        scopes = [
            scope
            for scope in REQUIRED
            if scope != PermissionEnum.MANAGE_OFFERING_AGENT.value
        ]
        use_pat(self.client, user, *scopes, bindings=bindings)
        response = self.client.get(
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "site_agent_config"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("offering_manager", "owner_as_manager", "offering_owner")
    def test_reads_its_offerings_from_the_service_provider(self, user_role):
        # An agent serving several offerings of a cluster loads them with one
        # call; the token's binding keeps the provider's other offerings out.
        self.offering.backend_id = "cluster-a-q"
        self.offering.save()
        sibling = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            type=marketplace_enums.SITE_AGENT_OFFERING,
            backend_id="cluster-a-u",
        )
        sibling.add_user(self.fixture.offering_manager, OfferingRole.MANAGER)
        self.use_template_token(user_role)
        response = self.client.get(
            marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider, "site_agent_config"
            ),
            {"backend_id_startswith": "cluster-a"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [row["offering_uuid"] for row in response.data], [self.offering.uuid.hex]
        )

    def test_cannot_read_a_sibling_offerings_configuration(self):
        other = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            type=marketplace_enums.SITE_AGENT_OFFERING,
        )
        other.add_user(self.fixture.offering_manager, OfferingRole.MANAGER)
        self.use_template_token("offering_manager")
        response = self.client.get(
            marketplace_factories.OfferingFactory.get_url(other, "site_agent_config")
        )
        # The token's binding keeps the sibling out of the queryset, before
        # any permission check.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("offering_manager", "offering_owner")
    def test_reports_policy_commands(self, user_role):
        policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        resource = marketplace_factories.ResourceFactory(offering=self.offering)
        self.use_template_token(user_role)
        response = self.client.post(
            SlurmPeriodicUsagePolicyFactory.get_url(policy, "report-command-result"),
            {"resource_uuid": resource.uuid.hex, "success": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("offering_manager", "offering_owner")
    def test_syncs_components_with_the_feature(self, user_role):
        for role in (CustomerRole.OWNER, OfferingRole.MANAGER):
            role.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        user = self.user(user_role)
        self.assertIn("sync_components", self.target(user)["features"])
        use_pat(
            self.client,
            user,
            *REQUIRED,
            PermissionEnum.UPDATE_OFFERING_COMPONENTS.value,
            bindings=self.template_bindings(user),
        )
        response = self.client.post(
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "create_offering_component"
            ),
            {
                "type": "agent_gpu",
                "name": "GPU",
                "measured_unit": "hours",
                "billing_type": marketplace_enums.BillingTypes.USAGE,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_token_with_staff_access_does_the_agents_work(self):
        staff = self.fixture.staff
        self.assertTrue(self.target(staff)["staff_access"])
        use_pat(self.client, staff, *REQUIRED, PermissionEnum.STAFF_ACCESS.value)
        order = self.order(marketplace_enums.OrderStates.PENDING_PROVIDER)
        response = self.client.post(
            marketplace_factories.OrderFactory.get_url(order, "approve_by_provider")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("offering_manager", "offering_owner")
    def test_cannot_edit_the_offering(self, user_role):
        self.use_template_token(user_role)
        response = self.client.patch(
            marketplace_factories.OfferingFactory.get_url(self.offering),
            {"name": "renamed"},
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )
        self.offering.refresh_from_db()
        self.assertNotEqual(self.offering.name, "renamed")

    def test_cannot_reach_a_sibling_offering(self):
        other = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            type=marketplace_enums.SITE_AGENT_OFFERING,
        )
        other.add_user(self.fixture.offering_manager, OfferingRole.MANAGER)
        self.use_template_token("offering_manager")
        order = self.order(marketplace_enums.OrderStates.PENDING_PROVIDER, other)
        response = self.client.post(
            marketplace_factories.OrderFactory.get_url(order, "approve_by_provider")
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        order.refresh_from_db()
        self.assertEqual(order.state, marketplace_enums.OrderStates.PENDING_PROVIDER)


@override_config(PAT_ENABLED=True)
class ReportCommandResultScopeTest(SiteAgentFixtureMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(
            self.policy, "report-command-result"
        )

    def test_consumer_is_told_nothing(self):
        # A 403 here would confirm the policy exists.
        resource = marketplace_factories.ResourceFactory(offering=self.offering)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url, {"resource_uuid": resource.uuid.hex, "success": True}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manager_of_another_offering_is_told_nothing(self):
        other = marketplace_factories.OfferingFactory(
            type=marketplace_enums.SITE_AGENT_OFFERING
        )
        user = structure_factories.UserFactory()
        other.add_user(user, OfferingRole.MANAGER)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            {
                "resource_uuid": marketplace_factories.ResourceFactory(
                    offering=self.offering
                ).uuid.hex,
                "success": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_resource_of_another_offering_is_refused(self):
        other_resource = marketplace_factories.ResourceFactory()
        history = policy_models.SlurmCommandHistory.objects.create(
            policy=self.policy,
            resource=other_resource,
            billing_period="2026-Q1",
            command_type="fairshare",
            description="desc",
            shell_command="sacctmgr ...",
            parameters={},
        )
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.post(
            self.url,
            {
                "resource_uuid": other_resource.uuid.hex,
                "success": False,
                "error_message": "boom",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        history.refresh_from_db()
        self.assertNotEqual(history.error_message, "boom")


class SiteAgentTemplateCoversAgentCallsTest(test.APISimpleTestCase):
    """Every action the site agent calls is checked against a template scope.

    The list is the agent's own calls (waldur-site-agent's SDK imports). When
    an action's permission changes, or the agent starts calling a new one,
    this fails until the template follows.
    """

    AGENT_ACTIONS = {
        marketplace_views.OrderViewSet: [
            "approve_by_provider",
            "reject_by_provider",
            "set_state_done",
            "set_state_erred",
            "set_backend_id",
        ],
        marketplace_views.ProviderResourceViewSet: [
            "set_backend_id",
            "set_as_ok",
            "set_as_erred",
            "refresh_last_sync",
            "set_limits",
            "set_backend_metadata",
            "set_endpoints",
            "set_membership_sync_statuses",
        ],
        marketplace_views.ComponentUsageViewSet: [
            "set_user_usage",
            "set_user_usages",
        ],
        marketplace_views.OfferingUsersViewSet: [
            "begin_creating",
            "set_ok",
            "set_error_creating",
            "set_error_deleting",
            "set_pending_account_linking",
            "set_pending_additional_validation",
            "set_validation_complete",
            "set_deleting",
            "set_deleted",
        ],
        policy_views.SlurmPeriodicUsagePolicyViewSet: ["report_command_result"],
    }

    def test_every_agent_action_needs_a_template_scope(self):
        for viewset, actions in self.AGENT_ACTIONS.items():
            for action in actions:
                with self.subTest(viewset=viewset.__name__, action=action):
                    checks = getattr(viewset, f"{action}_permissions")
                    permissions = {
                        getattr(check, "permission", None) for check in checks
                    }
                    self.assertTrue(
                        permissions & set(REQUIRED_SCOPES),
                        f"{viewset.__name__}.{action} checks {permissions}",
                    )


@ddt
@override_config(PAT_ENABLED=True)
class OfferingUsersSshKeysTest(SiteAgentFixtureMixin, test.APITestCase):
    """An offering manager, and its agent's token, list the SSH keys of the
    offering's users only; the provider's owner lists them all."""

    def setUp(self):
        super().setUp()
        for role in (CustomerRole.OWNER, OfferingRole.MANAGER):
            role.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_KEYS)
        self.offering.shared = True
        self.offering.save()
        self.fixture.resource.state = marketplace_models.Resource.States.OK
        self.fixture.resource.save()
        self.own_key = structure_factories.SshPublicKeyFactory(user=self.fixture.admin)

        # A sibling offering of the same provider, used by another project.
        other_offering = marketplace_factories.OfferingFactory(
            customer=self.offering.customer, shared=True
        )
        other_project = structure_factories.ProjectFactory()
        other_user = structure_factories.UserFactory()
        other_project.add_user(other_user, ProjectRole.ADMIN)
        marketplace_factories.ResourceFactory(
            offering=other_offering,
            project=other_project,
            state=marketplace_models.Resource.States.OK,
        )
        self.other_key = structure_factories.SshPublicKeyFactory(user=other_user)
        self.url = (
            f"/api/marketplace-service-providers/"
            f"{self.fixture.service_provider.uuid.hex}/keys/"
        )

    def keys(self):
        response = self.client.get(self.url)
        if response.status_code != status.HTTP_200_OK:
            return response.status_code
        return {row["uuid"] for row in response.data}

    def test_offering_manager_lists_its_offering_users_keys_only(self):
        self.client.force_authenticate(self.fixture.offering_manager)
        self.assertEqual(self.keys(), {self.own_key.uuid.hex})

    def test_owner_lists_every_key_of_the_provider(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        self.assertEqual(self.keys(), {self.own_key.uuid.hex, self.other_key.uuid.hex})

    def test_without_the_permission_it_is_refused(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.keys(), status.HTTP_403_FORBIDDEN)

    @data("offering_manager", "offering_owner")
    def test_agent_token_needs_the_ssh_keys_feature(self, user_role):
        user = getattr(self.fixture, user_role)
        target = self.target(user)
        self.assertIn("ssh_keys", target["features"])

        use_pat(self.client, user, *REQUIRED, bindings=[self.offering])
        self.assertEqual(self.keys(), status.HTTP_403_FORBIDDEN)

        use_pat(
            self.client,
            user,
            *REQUIRED,
            PermissionEnum.LIST_SERVICE_PROVIDER_KEYS.value,
            bindings=[self.offering],
        )
        self.assertEqual(self.keys(), {self.own_key.uuid.hex})


@override_config(PAT_ENABLED=True)
class DedicatedAgentUserTest(SiteAgentFixtureMixin, test.APITestCase):
    """Staff create a user that only manages the offering, and its token."""

    URL = "/api/personal-access-tokens/agent_user/"

    def post(self, user, grant=True, **overrides):
        self.client.force_authenticate(user)
        payload = {
            "template": SITE_AGENT_TEMPLATE_KEY,
            "target_type": "offering",
            "target_uuid": self.offering.uuid.hex,
            "name": "Site agent",
            "expires_at": (timezone.now() + timedelta(days=2)).isoformat(),
            **overrides,
        }
        extra = (
            reauthenticated(user, ReauthenticationAction.CREATE_PERSONAL_ACCESS_TOKEN)
            if grant
            else {}
        )
        return self.client.post(self.URL, payload, format="json", **extra)

    def test_staff_get_a_user_and_a_token_restricted_to_the_offering(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        response = self.post(self.fixture.staff, features=["sync_components"])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS.value, response.data["scopes"]
        )
        agent = User.objects.get(uuid=response.data["user_uuid"])
        self.assertFalse(agent.has_usable_password())
        self.assertTrue(agent.is_service_account)
        self.assertFalse(agent.is_staff)
        self.assertTrue(self.offering.has_user(agent, OfferingRole.MANAGER))
        self.assertEqual(
            [b["uuid"].replace("-", "") for b in response.data["allowed_scopes"]],
            [self.offering.uuid.hex],
        )

        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        order = marketplace_factories.OrderFactory(
            offering=self.offering,
            project=self.fixture.project,
            plan=marketplace_factories.PlanFactory(offering=self.offering),
            state=marketplace_enums.OrderStates.PENDING_PROVIDER,
        )
        approved = self.client.post(
            marketplace_factories.OrderFactory.get_url(order, "approve_by_provider")
        )
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.data)

        sibling = marketplace_factories.OfferingFactory(
            customer=self.offering.customer, type=marketplace_enums.SITE_AGENT_OFFERING
        )
        other_order = marketplace_factories.OrderFactory(
            offering=sibling,
            project=self.fixture.project,
            plan=marketplace_factories.PlanFactory(offering=sibling),
            state=marketplace_enums.OrderStates.PENDING_PROVIDER,
        )
        refused = self.client.post(
            marketplace_factories.OrderFactory.get_url(
                other_order, "approve_by_provider"
            )
        )
        self.assertIn(
            refused.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        )

    def test_the_audit_log_says_who_created_the_user_and_why(self):
        staff = self.fixture.staff
        response = self.post(staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        username = response.data["username"]
        recorded = list(
            Event.objects.filter(context__affected_user_username=username).values_list(
                "event_type", flat=True
            )
        )
        for event_type in (
            EventType.USER_CREATION_SUCCEEDED,
            EventType.DEDICATED_USER_CREATED,
            EventType.ROLE_GRANTED,
            EventType.PAT_CREATED,
        ):
            self.assertIn(event_type, recorded)
        # The account never had a password to change.
        self.assertNotIn(EventType.USER_PASSWORD_UPDATED, recorded)

        created = Event.objects.get(
            event_type=EventType.DEDICATED_USER_CREATED,
            context__affected_user_username=username,
        )
        self.assertEqual(created.context["creator_username"], staff.username)
        self.assertEqual(created.context["target_uuid"], self.offering.uuid.hex)
        granted = Event.objects.get(
            event_type=EventType.ROLE_GRANTED,
            context__affected_user_username=username,
        )
        self.assertIn("Dedicated", granted.message)

    def test_only_staff(self):
        response = self.post(self.fixture.offering_owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_not_with_a_token(self):
        use_pat(self.client, self.fixture.staff, PermissionEnum.STAFF_ACCESS.value)
        response = self.client.post(self.URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_needs_re_authentication(self):
        staff = self.fixture.staff
        before = User.objects.count()
        response = self.post(staff, grant=False)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), before)

    def test_a_role_that_does_not_qualify_leaves_nothing_behind(self):
        OfferingRole.MANAGER.permissions.filter(
            permission=PermissionEnum.LIST_ORDERS.value
        ).delete()
        staff = self.fixture.staff
        before = User.objects.count()
        response = self.post(staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), before)

    def test_a_feature_the_role_does_not_grant_leaves_nothing_behind(self):
        staff = self.fixture.staff
        before = User.objects.count()
        response = self.post(staff, features=["resource_api_keys"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), before)

    def test_a_taken_username_is_refused(self):
        response = self.post(self.fixture.staff, username=self.fixture.owner.username)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_see_the_username_a_dedicated_user_gets(self):
        self.offering.name = "SLURM HPC Cluster"
        self.offering.save()
        expected = f"site-agent-slurm-hpc-cluster-{self.offering.uuid.hex[:6]}"
        self.assertEqual(
            self.target(self.fixture.staff)["suggested_username"], expected
        )

        response = self.post(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["username"], expected)
        # Taken now: the next one is numbered.
        self.assertEqual(
            self.target(self.fixture.staff)["suggested_username"], f"{expected}-2"
        )

    def test_others_get_no_suggested_username(self):
        self.assertEqual(
            self.target(self.fixture.offering_manager)["suggested_username"], ""
        )

    def test_the_template_says_which_targets_take_a_dedicated_user(self):
        template = self.site_agent_template(self.fixture.staff)
        self.assertEqual(template["dedicated_user_types"], ["offering"])
