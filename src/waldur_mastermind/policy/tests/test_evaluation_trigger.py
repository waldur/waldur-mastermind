"""Policy events record what started the evaluation and who.

The event user of a policy action is the system robot, which hid the person
whose request started the evaluation, and nothing said which path evaluated
the policy. Every evaluation now records `trigger`, `trigger_object_uuid` where
there is one, and `initiated_by_*` for the request user.
"""

import datetime
import decimal
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework import status, test

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.logging import middleware as logging_middleware
from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.models import ProjectCredit
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import tasks, utils
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.structures import EvaluationTriggers
from waldur_mastermind.policy.tests import factories as policy_factories

LIMIT = 100
_APPLY_ASYNC = "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
_PROJECT_POLICY_PATH = "waldur_mastermind.policy.models.ProjectEstimatedCostPolicy"
_CUSTOMER_POLICY_PATH = "waldur_mastermind.policy.models.CustomerEstimatedCostPolicy"
_OFFERING_USAGE_POLICY_PATH = "waldur_mastermind.policy.models.OfferingUsagePolicy"


class EvaluationTriggerTest(test.APITestCase):
    def setUp(self):
        # Debounce keys live in the cache; one left by another test would
        # swallow a publish these tests expect.
        cache.clear()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        offering = self.resource.offering
        offering.plugin_options.update(
            {"supports_downscaling": True, "supports_pausing": True}
        )
        offering.save()
        month_start = core_utils.month_start(datetime.date.today())
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )

    def tearDown(self):
        logging_middleware.reset_event_context()
        super().tearDown()

    def _over_limit(self):
        # Earlier uncovered cost and a spent allocation: a TOTAL policy fires.
        previous = core_utils.month_start(datetime.date.today()) - datetime.timedelta(
            days=1
        )
        old_invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=previous.year,
            month=previous.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=old_invoice,
            project=self.project,
            resource=self.resource,
            unit_price=decimal.Decimal("7701.67"),
            quantity=1,
        )
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)

    def _charge(self, amount):
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=self.resource,
            unit_price=amount,
            quantity=1,
        )

    def _policy(self, **kwargs):
        defaults = dict(
            scope=self.project,
            limit_cost=LIMIT,
            actions="request_downscaling",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
            use_credit=True,
        )
        defaults.update(kwargs)
        return policy_factories.ProjectEstimatedCostPolicyFactory(**defaults)

    def _as_request_user(self, user):
        logging_middleware.set_event_context(user._get_log_context("user"))

    def _event(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type).latest(
            "created"
        )

    # Synchronous: the policy serializer's save().

    def test_api_save_keeps_the_robot_and_records_the_initiator(self):
        self._over_limit()
        staff = self.fixture.staff
        self.client.force_authenticate(staff)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                policy_factories.ProjectEstimatedCostPolicyFactory.get_list_url(),
                {
                    "scope": structure_factories.ProjectFactory.get_url(self.project),
                    "limit_cost": LIMIT,
                    "period": ProjectEstimatedCostPolicy.Periods.TOTAL,
                    "actions": "request_downscaling",
                    "use_credit": True,
                },
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        context = self._event("request_downscaling").context
        self.assertEqual(context["user_username"], "system_robot")
        self.assertEqual(context["initiated_by_uuid"], staff.uuid.hex)
        self.assertEqual(context["initiated_by_username"], staff.username)
        self.assertEqual(context["trigger"], EvaluationTriggers.POLICY_SAVED)
        self.assertEqual(context["trigger_object_uuid"], response.data["uuid"])

    # Dispatch: the handlers put the trigger into the event context while the
    # task is published; celeryconf carries that context, request user
    # included, to the worker. The task's arguments do not change.

    def _published_contexts(self, policy_path):
        return [context for args, context in self._published if args[0] == policy_path]

    def _capture_publishes(self):
        self._published = []

        def record(*args, **kwargs):
            self._published.append(
                (kwargs["args"], dict(logging_middleware.get_event_context() or {}))
            )

        return mock.patch(_APPLY_ASYNC, side_effect=record)

    @override_settings(WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
    def test_invoice_item_save_publishes_its_trigger_and_user(self):
        user = self.fixture.owner
        self._as_request_user(user)
        with self._capture_publishes():
            with self.captureOnCommitCallbacks(execute=True):
                item = self._charge(10)

        for path in (_PROJECT_POLICY_PATH, _CUSTOMER_POLICY_PATH):
            published = [args for args, _ in self._published if args[0] == path]
            self.assertEqual(len(published), 1, self._published)
            # The task signature every worker version accepts.
            self.assertEqual(len(published[0]), 2)
            (context,) = self._published_contexts(path)
            self.assertEqual(context["trigger"], EvaluationTriggers.INVOICE_ITEM_SAVED)
            self.assertEqual(context["trigger_object_uuid"], item.uuid.hex)
            self.assertEqual(context["user_uuid"], user.uuid.hex)
        # Publishing leaves the request's own context as it was.
        self.assertNotIn("trigger", logging_middleware.get_event_context())

    @override_settings(WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
    def test_credit_change_publishes_its_trigger(self):
        self._as_request_user(self.fixture.owner)
        # Everything that publishes stays under the mock: nothing reaches a
        # real broker.
        with self._capture_publishes():
            invoices_factories.CustomerCreditFactory(
                customer=self.customer, value=100000
            )
            project_credit = invoices_factories.ProjectCreditFactory(
                project=self.project, value=2900
            )
            # Creating the credits already published and claimed the debounce
            # keys; start the window afresh so the change is published itself.
            self._published.clear()
            cache.clear()
            with self.captureOnCommitCallbacks(execute=True):
                project_credit.value = 5000
                project_credit.save()

        (context,) = self._published_contexts(_PROJECT_POLICY_PATH)
        self.assertEqual(context["trigger"], EvaluationTriggers.CREDIT_CHANGED)
        self.assertEqual(context["trigger_object_uuid"], project_credit.uuid.hex)

    @override_settings(WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
    def test_object_created_with_a_string_uuid_still_saves(self):
        # The structure import creates objects with `uuid="…"`; the attribute
        # stays a string, and reading `.uuid.hex` from it made the save fail.
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        cache.clear()
        with self._capture_publishes():
            with self.captureOnCommitCallbacks(execute=True):
                credit = ProjectCredit.objects.create(
                    uuid="550e8400-e29b-41d4-a716-446655440001",
                    project=self.project,
                    value=10,
                )

        self.assertTrue(ProjectCredit.objects.filter(pk=credit.pk).exists())
        (context,) = self._published_contexts(_PROJECT_POLICY_PATH)
        self.assertEqual(
            context["trigger_object_uuid"], "550e8400e29b41d4a716446655440001"
        )

    @override_settings(WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
    def test_usage_report_publishes_its_trigger(self):
        with self._capture_publishes():
            with self.captureOnCommitCallbacks(execute=True):
                usage = marketplace_factories.ComponentUsageFactory(
                    resource=self.resource
                )

        (context,) = self._published_contexts(_OFFERING_USAGE_POLICY_PATH)
        self.assertEqual(context["trigger"], EvaluationTriggers.USAGE_REPORTED)
        self.assertEqual(context["trigger_object_uuid"], usage.uuid.hex)
        # Reported outside any request: nobody to record.
        self.assertNotIn("initiated_by_uuid", context)

    def test_task_records_the_published_trigger_and_initiator(self):
        # The worker side: celeryconf.bind_event_context restores the context
        # the request published, then the unchanged task runs.
        self._over_limit()
        policy = self._policy()
        item = self._charge(10)
        owner = self.fixture.owner
        logging_middleware.set_event_context(
            {
                **owner._get_log_context("user"),
                "trigger": EvaluationTriggers.INVOICE_ITEM_SAVED,
                "trigger_object_uuid": item.uuid.hex,
            }
        )

        tasks.evaluate_policies_async(_PROJECT_POLICY_PATH, {"pk": policy.pk})

        context = self._event("request_downscaling").context
        self.assertEqual(context["user_username"], "system_robot")
        self.assertEqual(context["initiated_by_uuid"], owner.uuid.hex)
        self.assertEqual(context["initiated_by_username"], owner.username)
        self.assertEqual(context["trigger"], EvaluationTriggers.INVOICE_ITEM_SAVED)
        self.assertEqual(context["trigger_object_uuid"], item.uuid.hex)

    def test_system_robot_is_never_the_initiator(self):
        # Background jobs such as monthly invoicing run as the system robot.
        self._over_limit()
        policy = self._policy()
        robot = core_utils.get_system_robot()
        logging_middleware.set_event_context(robot._get_log_context("user"))

        utils.evaluate_policies([policy], trigger=EvaluationTriggers.INVOICE_ITEM_SAVED)

        context = self._event("request_downscaling").context
        self.assertEqual(context["user_username"], "system_robot")
        self.assertNotIn("initiated_by_uuid", context)
        self.assertNotIn("initiated_by_username", context)

    def test_new_trigger_drops_the_old_object(self):
        # A context still carrying an earlier trigger's object must not lend it
        # to an evaluation started by something else.
        self._over_limit()
        policy = self._policy()
        logging_middleware.set_event_context(
            {
                "trigger": EvaluationTriggers.INVOICE_ITEM_SAVED,
                "trigger_object_uuid": "0" * 32,
            }
        )

        utils.evaluate_policies([policy], trigger=EvaluationTriggers.PERIODIC_SWEEP)

        context = self._event("request_downscaling").context
        self.assertEqual(context["trigger"], EvaluationTriggers.PERIODIC_SWEEP)
        self.assertNotIn("trigger_object_uuid", context)

    # Periodic sweep and its reconcile pass.

    def test_periodic_sweep_records_its_trigger(self):
        self._over_limit()
        self._policy()

        tasks.check_polices()

        context = self._event("request_downscaling").context
        self.assertEqual(context["trigger"], EvaluationTriggers.PERIODIC_SWEEP)
        self.assertNotIn("initiated_by_uuid", context)

    def test_reconcile_records_its_trigger(self):
        # Already fired while the resource could not be paused; the sweep
        # reconciles it once the offering supports pausing.
        self.resource.state = ResourceStates.OK
        self.resource.paused = False
        self.resource.save()
        self._policy(
            actions="request_pausing",
            limit_cost=0,
            period=ProjectEstimatedCostPolicy.Periods.MONTH_1,
            use_credit=False,
            has_fired=True,
        )
        self._charge(10)

        tasks.check_polices()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        context = self._event("request_pausing").context
        self.assertEqual(context["trigger"], EvaluationTriggers.RECONCILE)

    # Not an evaluation: threshold actions on a resource save.

    def test_threshold_action_on_resource_save_records_its_trigger(self):
        self._policy(
            actions="block_creation_of_new_resources",
            limit_cost=0,
            use_credit=False,
            has_fired=True,
        )
        user = self.fixture.owner
        self._as_request_user(user)

        with self.assertRaises(PolicyException):
            marketplace_factories.ResourceFactory(
                project=self.project, offering=self.resource.offering
            )

        context = self._event("block_creation_of_new_resources").context
        self.assertEqual(context["trigger"], EvaluationTriggers.RESOURCE_SAVED)
        # The request user is the actor here, not the system robot.
        self.assertEqual(context["user_uuid"], user.uuid.hex)

    def test_threshold_action_inside_an_evaluation_keeps_its_trigger(self):
        self._policy(
            actions="block_creation_of_new_resources",
            limit_cost=0,
            use_credit=False,
            has_fired=True,
        )
        with utils.system_actor_event_context(EvaluationTriggers.CREDIT_CHANGED):
            with self.assertRaises(PolicyException):
                marketplace_factories.ResourceFactory(
                    project=self.project, offering=self.resource.offering
                )

        context = self._event("block_creation_of_new_resources").context
        self.assertEqual(context["trigger"], EvaluationTriggers.CREDIT_CHANGED)

    def test_trigger_carried_into_a_later_task_is_not_kept(self):
        # Celery copies the event context into every task published during an
        # evaluation; such a task is not inside that evaluation, so its own
        # resource saves are recorded as `resource_saved`.
        self._policy(
            actions="block_creation_of_new_resources",
            limit_cost=0,
            use_credit=False,
            has_fired=True,
        )
        logging_middleware.set_event_context(
            {
                "trigger": EvaluationTriggers.CREDIT_CHANGED,
                "trigger_object_uuid": "0" * 32,
            }
        )

        with self.assertRaises(PolicyException):
            resource = marketplace_factories.ResourceFactory.build(
                project=self.project, offering=self.resource.offering
            )
            resource.save()

        context = self._event("block_creation_of_new_resources").context
        self.assertEqual(context["trigger"], EvaluationTriggers.RESOURCE_SAVED)
        self.assertNotEqual(context.get("trigger_object_uuid"), "0" * 32)

    # Who is recorded as the initiator.

    def test_no_robot_account_is_an_initiator(self):
        self._over_limit()
        policy = self._policy()
        robot = structure_factories.UserFactory(username="openportal_robot")
        logging_middleware.set_event_context(robot._get_log_context("user"))

        utils.evaluate_policies([policy], trigger=EvaluationTriggers.CREDIT_CHANGED)

        context = self._event("request_downscaling").context
        self.assertNotIn("initiated_by_uuid", context)
        # Already a robot: not swapped for the system robot.
        self.assertEqual(context["user_username"], "openportal_robot")

    def test_person_acting_now_wins_over_a_carried_initiator(self):
        self._over_limit()
        policy = self._policy()
        owner = self.fixture.owner
        logging_middleware.set_event_context(
            {
                **owner._get_log_context("user"),
                "initiated_by_uuid": "0" * 32,
                "initiated_by_username": "someone-earlier",
            }
        )

        utils.evaluate_policies([policy], trigger=EvaluationTriggers.CREDIT_CHANGED)

        context = self._event("request_downscaling").context
        self.assertEqual(context["initiated_by_uuid"], owner.uuid.hex)
        self.assertEqual(context["initiated_by_username"], owner.username)

    def _impersonated_context(self, impersonator, user):
        impersonated = core_models.ImpersonatedUser.all_objects.get(pk=user.pk)
        impersonated.impersonator = impersonator
        return impersonated._get_log_context("user")

    def test_impersonator_is_the_initiator(self):
        # The impersonator is accountable, not the account being acted as; that
        # account is kept beside it, as revisions record it in their comment.
        self._over_limit()
        policy = self._policy()
        staff, owner = self.fixture.staff, self.fixture.owner
        logging_middleware.set_event_context(self._impersonated_context(staff, owner))

        utils.evaluate_policies([policy], trigger=EvaluationTriggers.CREDIT_CHANGED)

        context = self._event("request_downscaling").context
        self.assertEqual(context["user_username"], "system_robot")
        self.assertNotIn("user_impersonator_uuid", context)
        self.assertEqual(context["initiated_by_uuid"], staff.uuid.hex)
        self.assertEqual(context["initiated_by_username"], staff.username)
        self.assertEqual(context["initiated_as_username"], owner.username)

    def test_impersonation_carries_into_a_nested_block(self):
        # A task published from inside an evaluation carries the robot's context
        # with the initiator fields; the account acted as must survive it.
        self._over_limit()
        policy = self._policy()
        staff, owner = self.fixture.staff, self.fixture.owner
        logging_middleware.set_event_context(self._impersonated_context(staff, owner))

        with utils.system_actor_event_context(EvaluationTriggers.CREDIT_CHANGED):
            utils.evaluate_policies([policy])

        context = self._event("request_downscaling").context
        self.assertEqual(context["initiated_by_uuid"], staff.uuid.hex)
        self.assertEqual(context["initiated_as_username"], owner.username)

    def test_no_account_acted_as_without_impersonation(self):
        self._over_limit()
        policy = self._policy()
        self._as_request_user(self.fixture.owner)

        utils.evaluate_policies([policy], trigger=EvaluationTriggers.CREDIT_CHANGED)

        self.assertNotIn(
            "initiated_as_username", self._event("request_downscaling").context
        )

    # SLURM periodic-usage policies.

    @override_settings(WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
    def test_slurm_usage_report_publishes_its_trigger(self):
        policy_factories.SlurmPeriodicUsagePolicyFactory(scope=self.resource.offering)
        published = []

        def record(*args, **kwargs):
            published.append(dict(logging_middleware.get_event_context() or {}))

        with mock.patch(
            "waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.apply_async",
            side_effect=record,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                usage = marketplace_factories.ComponentUsageFactory(
                    resource=self.resource
                )

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["trigger"], EvaluationTriggers.USAGE_REPORTED)
        self.assertEqual(published[0]["trigger_object_uuid"], usage.uuid.hex)

    # Not an evaluation: a deleted policy's actions being reset.

    def test_policy_deletion_survives_a_failing_robot_lookup(self):
        # The reset is a side effect: whatever goes wrong in it is logged and
        # must never abort the delete it runs for.
        policy = self._policy()
        self._as_request_user(self.fixture.owner)

        with mock.patch(
            "waldur_mastermind.policy.utils.get_system_robot",
            side_effect=RuntimeError("database unavailable"),
        ):
            policy.delete()

        self.assertFalse(ProjectEstimatedCostPolicy.objects.filter(pk=policy.pk))

    def test_policy_deletion_resets_record_their_trigger_and_initiator(self):
        self._over_limit()
        policy = self._policy()
        utils.evaluate_policies([policy])
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        owner = self.fixture.owner
        self._as_request_user(owner)
        policy_uuid = policy.uuid.hex

        policy.delete()

        context = self._event("reset_downscaling").context
        self.assertEqual(context["trigger"], EvaluationTriggers.POLICY_DELETED)
        self.assertEqual(context["trigger_object_uuid"], policy_uuid)
        self.assertEqual(context["user_username"], "system_robot")
        self.assertEqual(context["initiated_by_uuid"], owner.uuid.hex)
