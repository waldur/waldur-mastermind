from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Feature
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import (
    order_flow_description,
    order_rules,
    permissions,
    utils,
)
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    REMOTE_OFFERING,
    SCRIPT_OFFERING,
    SITE_AGENT_OFFERING,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories

ORDER_KIND_FOR_TYPE = {
    OrderTypes.CREATE: "create",
    OrderTypes.TERMINATE: "terminate",
}


class OrderFlowAgreesWithEngineTest(test.APITestCase):
    """For the same situation, the description's review steps and the
    engine's routing must name the same deciding rule and outcome."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)
        # Created up front: the roles the orderers are described by.
        ProjectRole.ADMIN, ProjectRole.MEMBER

    def offering(self, customer=None, **kwargs):
        kwargs.setdefault("shared", True)
        kwargs.setdefault("billable", True)
        return factories.OfferingFactory(
            customer=customer or structure_factories.CustomerFactory(), **kwargs
        )

    def assert_agrees(self, engine_skip, engine_rule, simulation):
        reached = [o for o in simulation.outcomes if o.reached]
        keys = [o.rule.key for o in reached]
        self.assertIn(engine_rule.key, keys)
        engine_index = keys.index(engine_rule.key)
        # Nothing the description says certainly holds may come before the
        # rule the engine applied.
        for outcome in reached[:engine_index]:
            self.assertIsNot(outcome.holds, True, outcome.rule.key)
        self.assertIsNot(reached[engine_index].holds, False)
        if simulation.verdict != "depends":
            self.assertEqual(simulation.verdict == "skipped", engine_skip)

    def check_consumer(self, order, orderer):
        engine_skip, engine_rule = order_rules.decide(
            order_rules.CONSUMER_RULES, permissions.ConsumerReviewFacts(order)
        )
        self.assertEqual(
            engine_skip, permissions.order_should_not_be_reviewed_by_consumer(order)
        )
        kind = order_flow_description.ORDER_KINDS_MAP[ORDER_KIND_FOR_TYPE[order.type]]
        facts = order_flow_description.SimulatedConsumerFacts(
            order.offering, kind, orderer
        )
        simulation = order_rules.simulate(order_rules.CONSUMER_RULES, facts)
        self.assert_agrees(engine_skip, engine_rule, simulation)
        return engine_rule.key, simulation.verdict

    def order(self, offering, user, order_type=OrderTypes.CREATE, **kwargs):
        return factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=user,
            type=order_type,
            state=OrderStates.PENDING_CONSUMER,
            **kwargs,
        )

    def role(self, name):
        return order_flow_description.make_orderer("role", name)

    def test_staff(self):
        order = self.order(self.offering(), self.fixture.staff)
        result = self.check_consumer(
            order, order_flow_description.make_orderer("staff")
        )
        self.assertEqual(result, ("staff", "skipped"))

    def test_owner_approves_own_order(self):
        order = self.order(self.offering(), self.fixture.owner)
        result = self.check_consumer(order, self.role(RoleEnum.CUSTOMER_OWNER.value))
        self.assertEqual(result, ("approver_orders", "skipped"))

    def test_manager_waits_for_approval(self):
        order = self.order(self.offering(), self.fixture.manager)
        result = self.check_consumer(order, self.role(RoleEnum.PROJECT_MANAGER.value))
        self.assertEqual(result, ("approver_orders", "required"))

    def test_auto_approve_role(self):
        offering = self.offering(
            plugin_options={"auto_approve_for_roles": [RoleEnum.PROJECT_MANAGER.value]}
        )
        order = self.order(offering, self.fixture.manager)
        result = self.check_consumer(order, self.role(RoleEnum.PROJECT_MANAGER.value))
        self.assertEqual(result, ("auto_approve_role", "skipped"))

    def test_disabled_auto_approval_holds_owner(self):
        offering = self.offering(plugin_options={"disable_autoapprove": True})
        order = self.order(offering, self.fixture.owner)
        result = self.check_consumer(order, self.role(RoleEnum.CUSTOMER_OWNER.value))
        self.assertEqual(result, ("autoapprove_disabled", "required"))

    def test_disabled_auto_approval_does_not_apply_to_termination(self):
        # Depends: a project manager may also own the provider, and an owner
        # of the provider terminates without consumer review.
        offering = self.offering(plugin_options={"disable_autoapprove": True})
        order = self.order(offering, self.fixture.manager, OrderTypes.TERMINATE)
        result = self.check_consumer(order, self.role(RoleEnum.PROJECT_MANAGER.value))
        self.assertEqual(result, ("approver_orders", "depends"))

    def test_purchase_order_hold_overrides_staff(self):
        offering = self.offering(plugin_options={"require_purchase_order_upload": True})
        order = self.order(offering, self.fixture.staff)
        result = self.check_consumer(
            order, order_flow_description.make_orderer("staff")
        )
        self.assertEqual(result, ("purchase_order_hold", "depends"))

    def test_private_offering(self):
        offering = self.offering(
            customer=self.fixture.customer, shared=False, billable=False
        )
        order = self.order(offering, self.fixture.manager)
        result = self.check_consumer(order, self.role(RoleEnum.PROJECT_MANAGER.value))
        self.assertEqual(result, ("private_offering", "skipped"))

    def test_proposal_allocation(self):
        order = self.order(
            self.offering(),
            self.fixture.manager,
            placed_automatically=True,
            consumer_reviewed_by=self.fixture.owner,
        )
        result = self.check_consumer(
            order, order_flow_description.make_orderer("proposal_allocation")
        )
        self.assertEqual(result, ("placed_with_review", "skipped"))

    def test_provider_project(self):
        offering = self.offering(
            customer=self.fixture.customer,
            plugin_options={"auto_approve_in_service_provider_projects": True},
        )
        order = self.order(offering, self.fixture.owner)
        result = self.check_consumer(
            order, order_flow_description.make_orderer("provider_owner")
        )
        self.assertEqual(result, ("service_provider_project", "skipped"))

    def test_provider_owner_terminates(self):
        offering = self.offering(customer=self.fixture.customer)
        order = self.order(offering, self.fixture.owner, OrderTypes.TERMINATE)
        result = self.check_consumer(
            order, order_flow_description.make_orderer("provider_owner")
        )
        self.assertEqual(result, ("provider_owner_terminates", "skipped"))

    def check_provider(self, offering, approver, orderer, consumer_verdict):
        engine_skip, engine_rule = order_rules.decide(
            order_rules.PROVIDER_RULES, utils.ProviderReviewFacts(offering, approver)
        )
        self.assertEqual(
            engine_skip, utils.offering_skips_provider_review(offering, approver)
        )
        facts = order_flow_description.SimulatedProviderFacts(
            offering, orderer, consumer_verdict
        )
        simulation = order_rules.simulate(order_rules.PROVIDER_RULES, facts)
        self.assert_agrees(engine_skip, engine_rule, simulation)
        return engine_rule.key, simulation.verdict

    def test_provider_review_by_type(self):
        staff = order_flow_description.make_orderer("staff")
        manager = self.role(RoleEnum.PROJECT_MANAGER.value)
        cases = (
            (BASIC_OFFERING, {}, self.fixture.staff, staff, ("basic", "required")),
            (
                SITE_AGENT_OFFERING,
                {},
                self.fixture.staff,
                staff,
                ("site_agent", "required"),
            ),
            (SCRIPT_OFFERING, {}, self.fixture.manager, manager, ("script", "skipped")),
            (
                SCRIPT_OFFERING,
                {"auto_approve_marketplace_script": False},
                self.fixture.staff,
                staff,
                ("script", "required"),
            ),
            (
                REMOTE_OFFERING,
                {"auto_approve_remote_orders": True},
                self.fixture.manager,
                manager,
                ("remote_auto_approve", "skipped"),
            ),
            (
                REMOTE_OFFERING,
                {},
                self.fixture.staff,
                staff,
                ("remote_provider_owner", "skipped"),
            ),
            (
                REMOTE_OFFERING,
                {},
                self.fixture.manager,
                manager,
                ("remote", "depends"),
            ),
            (
                "OpenStack.Tenant",
                {},
                self.fixture.manager,
                manager,
                ("automatic", "skipped"),
            ),
        )
        for offering_type, options, approver, orderer, expected in cases:
            with self.subTest(offering_type=offering_type, options=options):
                offering = self.offering(type=offering_type, plugin_options=options)
                result = self.check_provider(offering, approver, orderer, "skipped")
                self.assertEqual(result, expected)


class OrderFlowDescriptionApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.provider = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.provider.customer,
            type=BASIC_OFFERING,
            shared=True,
            billable=True,
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, "order_flow_description"
        )

    def get(self, user=None, **params):
        self.client.force_authenticate(user or self.provider.owner)
        return self.client.get(self.url, params)

    def step(self, data, key):
        return next(step for step in data["steps"] if step["key"] == key)

    def test_provider_owner_can_read(self):
        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["order_kind"], "create")
        self.assertEqual(
            [step["key"] for step in response.data["steps"]],
            [
                "placed",
                "consumer_review",
                "project_start",
                "provider_review",
                "start_date",
                "execution",
                "done",
            ],
        )

    def test_other_users_cannot_read(self):
        response = self.get(self.fixture.owner)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_basic_offering_is_reviewed_by_provider_and_done_on_approval(self):
        data = self.get(orderer="staff").data
        provider = self.step(data, "provider_review")
        self.assertEqual(provider["status"], "runs")
        self.assertEqual(self.step(data, "consumer_review")["status"], "skipped")
        execution = self.step(data, "execution")
        self.assertEqual(execution["mode"], "sync")
        terminals = {t["key"]: t for t in data["terminals"]}
        self.assertEqual(terminals["rejected"]["from_steps"], ["provider_review"])
        self.assertTrue(terminals["erred"]["retry"])

    def test_order_kinds_follow_offering_type_and_settings(self):
        kinds = {k["key"]: k for k in self.get().data["order_kinds"]}
        self.assertTrue(kinds["create"]["supported"])
        self.assertTrue(kinds["terminate"]["supported"])
        self.assertFalse(kinds["restore"]["supported"])
        self.assertEqual(
            kinds["restore"]["sources"][0]["field"], "can_restore_resource"
        )
        self.assertTrue(kinds["restore"]["sources"][0]["editable"])

    def test_automatic_termination_is_approved_by_waldur(self):
        data = self.get(order_kind="auto_terminate").data
        self.assertEqual(data["orderer"]["kind"], "system")
        self.assertEqual(self.step(data, "consumer_review")["status"], "skipped")
        self.assertEqual(self.step(data, "provider_review")["status"], "runs")

    @override_config(ENABLE_ORDER_START_DATE=False)
    def test_platform_settings_are_not_editable(self):
        step = self.step(self.get().data, "start_date")
        self.assertEqual(step["status"], "skipped")
        self.assertEqual(step["sources"][0]["kind"], "constance")
        self.assertFalse(step["sources"][0]["editable"])

    @override_config(ENABLE_ORDER_START_DATE=True)
    def test_start_date_is_a_branch_when_enabled(self):
        self.assertEqual(
            self.step(self.get().data, "start_date")["status"], "conditional"
        )

    def test_anyone_draws_consumer_review_as_branch(self):
        step = self.step(self.get().data, "consumer_review")
        self.assertEqual(step["status"], "conditional")

    def remote_offering(self, **options):
        self.offering.type = REMOTE_OFFERING
        self.offering.plugin_options = options
        self.offering.save()

    def test_proposal_acceptor_may_skip_remote_provider_review(self):
        Feature.objects.update_or_create(
            key=order_flow_description.CALL_MANAGEMENT_FEATURE,
            defaults={"value": True},
        )
        self.remote_offering()
        step = self.step(
            self.get(orderer="proposal_allocation").data, "provider_review"
        )
        self.assertEqual(step["status"], "conditional")

    def test_automatic_termination_of_remote_resource_may_need_provider(self):
        self.remote_offering()
        step = self.step(self.get(order_kind="auto_terminate").data, "provider_review")
        self.assertEqual(step["status"], "conditional")

    def test_project_rule_is_a_branch_when_purchase_order_is_required(self):
        self.offering.plugin_options = {"require_purchase_order_upload": True}
        self.offering.save()
        step = self.step(self.get().data, "consumer_review")
        self.assertIn("project_auto_approval", [r["key"] for r in step["rules"]])

    def test_role_orderer_needs_a_role(self):
        response = self.get(orderer="role")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_orderer(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        response = self.get(orderer="role", role=RoleEnum.PROJECT_MANAGER.value)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["orderer"]["role"], "PROJECT.MANAGER")

    def test_stats_only_on_request(self):
        factories.OrderFactory(
            offering=self.offering, project=self.fixture.project, state=OrderStates.DONE
        )
        self.assertIsNone(self.get().data["stats"])
        stats = self.get(with_stats=True).data["stats"]
        counts = {row["state"]: row["count"] for row in stats["states"]}
        self.assertEqual(counts.get("done"), 1)


class ProviderReviewReadsUserOnlyWhenNeededTest(test.APITestCase):
    def test_non_remote_types_do_not_look_at_the_approver(self):
        for offering_type in ("OpenStack.Tenant", SCRIPT_OFFERING, BASIC_OFFERING):
            with self.subTest(offering_type=offering_type):
                offering = factories.OfferingFactory(type=offering_type)
                with self.assertNumQueries(0):
                    # No user at all: the creator's account may be deleted.
                    utils.offering_skips_provider_review(offering, None)


class OrderFlowOrderersTest(test.APITestCase):
    """Only orderers that can exist on this deployment are offered."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.provider = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.provider.customer)
        self.url = factories.OfferingFactory.get_url(
            self.offering, "order_flow_description"
        )
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        # A role that cannot place orders at all.
        ProjectRole.MEMBER
        self.client.force_authenticate(self.provider.owner)

    def orderers(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {(o["kind"], o["role"]) for o in response.data["orderers"]}

    def test_proposal_allocation_needs_call_management(self):
        self.assertNotIn(("proposal_allocation", None), self.orderers())
        response = self.client.get(self.url, {"orderer": "proposal_allocation"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        Feature.objects.update_or_create(
            key=order_flow_description.CALL_MANAGEMENT_FEATURE,
            defaults={"value": True},
        )
        self.assertIn(("proposal_allocation", None), self.orderers())

    def test_only_roles_that_may_order(self):
        orderers = self.orderers()
        self.assertIn(("role", RoleEnum.PROJECT_MANAGER.value), orderers)
        self.assertNotIn(("role", RoleEnum.PROJECT_MEMBER.value), orderers)
        response = self.client.get(
            self.url, {"orderer": "role", "role": RoleEnum.PROJECT_MEMBER.value}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_restricted_offering_offers_only_its_roles(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.offering.plugin_options = {
            "restricted_to_roles": [RoleEnum.CUSTOMER_OWNER.value]
        }
        self.offering.save()
        roles = {role for kind, role in self.orderers() if kind == "role"}
        self.assertEqual(roles, {RoleEnum.CUSTOMER_OWNER.value})
