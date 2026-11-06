"""What keeps a dry run, and the checks built on it, inside its bounds.

A run arms its tripwires around everything it does, the setup of a template
check included; it stays within its time budget however many people a step
asks for; it never shares a role it created with another thread; and a call's
scenarios run in the background, keeping their results only when every
scenario could run.
"""

import socket
import threading
import traceback
from unittest import mock

import httpx
import kombu
from celery import current_app
from celery.app.task import Task as CeleryTask
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.cache import cache
from django.core.mail import EmailMessage
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import utils as core_utils
from waldur_core.permissions.models import Role, RoleManager, UserRole
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    call_templates,
    dry_run,
    models,
    tasks,
    tripwires,
    workflow_scenarios,
)
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_workflow_dry_run import DryRunMixin
from waldur_mastermind.proposal.tests.test_workflow_scenarios import (
    SCENARIO,
    ScenarioTestBase,
    make_template,
)

FREEIPA_GROUP_SYNC = {
    "FREEIPA_ENABLED": True,
    "FREEIPA_GROUP_SYNCHRONIZATION_ENABLED": True,
}


class CeleryCalls:
    """Every task queued, and whether the run was armed as it was."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        recorder = self

        def apply_async(task, *args, **kwargs):
            recorder.calls.append((task.name, tripwires.is_armed()))

        self.patch = mock.patch.object(CeleryTask, "apply_async", apply_async)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class GroupSyncTest(DryRunMixin, test.APITestCase):
    """With FreeIPA group sync on, role grants queue a sync: none of a check
    or a dry run may."""

    def setUp(self):
        super().setUp()
        syncing = mock.patch("waldur_freeipa.utils.is_syncing", return_value=False)
        syncing.start()
        self.addCleanup(syncing.stop)

    def test_checking_a_template_document_queues_nothing(self):
        before = Customer.objects.count()
        with override_config(**FREEIPA_GROUP_SYNC), CeleryCalls() as celery:
            warnings = call_templates.dry_run(
                call_templates.load("single_panel_review"), self.staff
            )
        self.assertEqual(celery.calls, [])
        self.assertIsInstance(warnings, list)
        self.assertEqual(Customer.objects.count(), before)

    def test_a_dry_run_works_with_group_sync_on(self):
        call = self.import_template("single_panel_review", offerings=1)
        with override_config(**FREEIPA_GROUP_SYNC), CeleryCalls() as celery:
            data = self.ok(call)
        self.assertEqual(celery.calls, [])
        self.assertTrue(data["steps"])


class FakeClock:
    """A clock standing still until ``step`` is set, then that many seconds
    further on at every reading."""

    def __init__(self):
        self.now = 0.0
        self.step = 0.0

    def monotonic(self):
        self.now += self.step
        return self.now


class BudgetTest(DryRunMixin, test.APITestCase):
    def review_step(self, call):
        step = (
            models.CallWorkflowStep.objects.filter(
                call=call,
                is_enabled=True,
                contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            )
            .order_by("order")
            .first()
        )
        self.assertIsNotNone(step)
        actions, data = self.walk(
            call,
            stop=lambda d: any(
                s["step"] == step.key and s["outcomes"] for s in d["next"]["steps"]
            ),
        )
        following = next(s for s in data["next"]["steps"] if s["step"] == step.key)
        outcome = next(
            o for o in following["outcomes"] if o not in {"rejected", "declined"}
        )
        return step, actions + [{"step": step.key, "outcome": outcome}]

    def test_the_budget_holds_inside_one_action(self):
        call = self.import_template("single_panel_review", offerings=1)
        step, actions = self.review_step(call)
        models.CallWorkflowStep.objects.filter(pk=step.pk).update(
            min_contributions=models.MAX_STEP_PEOPLE, max_contributions=None
        )
        clock = FakeClock()
        original = dry_run._Run.ensure_reviews

        def ensure_reviews(run, instance, *args, **kwargs):
            # A second goes by with every reading from here on: the budget
            # runs out while the step's reviewers are being made.
            if instance.call_step.key == step.key:
                clock.step = 1.0
            return original(run, instance, *args, **kwargs)

        frames = []
        with (
            mock.patch.object(dry_run, "time", clock),
            mock.patch.object(dry_run._Run, "ensure_reviews", ensure_reviews),
        ):
            try:
                dry_run.run(call, {"actions": actions})
            except dry_run.DryRunTimeout as error:
                frames = [f.name for f in traceback.extract_tb(error.__traceback__)]
        self.assertIn("person", frames)

    def test_a_call_asking_for_more_people_than_allowed_is_not_run(self):
        call = self.import_template("single_panel_review", offerings=1)
        step, actions = self.review_step(call)
        # Written around the bound, as a call configured before it may be.
        models.CallWorkflowStep.objects.filter(pk=step.pk).update(
            min_contributions=models.MAX_STEP_PEOPLE + 50, max_contributions=None
        )
        response = self.dry(call, actions)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(models.MAX_STEP_PEOPLE), str(response.data))

    def test_a_run_makes_no_more_people_than_its_bound(self):
        call = self.import_template("single_panel_review", offerings=1)
        step, actions = self.review_step(call)
        models.CallWorkflowStep.objects.filter(pk=step.pk).update(
            min_contributions=3, max_contributions=None
        )
        made = []
        original = dry_run._Run.person

        def person(run, label, single=False):
            made.append(label)
            return original(run, label, single)

        logged = []
        original_capped = dry_run._Run.capped

        def capped(run, needed, instance):
            result = original_capped(run, needed, instance)
            logged.extend(e for e in run.log if e["kind"] == "capped")
            return result

        with (
            mock.patch.object(dry_run, "MAX_PEOPLE_PER_STEP", 2),
            mock.patch.object(dry_run._Run, "person", person),
            mock.patch.object(dry_run._Run, "capped", capped),
        ):
            response = self.dry(call, actions)
        self.assertEqual(made.count("reviewer"), 2, response.data)
        self.assertIn("asks for 3 people", logged[0]["text"])


class PeopleBoundTest(DryRunMixin, test.APITestCase):
    def test_a_step_asks_for_at_most_the_bound(self):
        call = self.import_template("single_panel_review", offerings=0)
        call.add_user(self.fixture.call_manager, "CALL.MANAGER")
        step = call.workflow_steps.filter(
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS
        ).first()
        self.client.force_authenticate(self.staff)
        url = factories.CallWorkflowStepFactory.get_url(call, step)
        for field in ("min_contributions", "max_contributions", "min_reviewers"):
            with self.subTest(field=field):
                response = self.client.patch(
                    url, {field: models.MAX_STEP_PEOPLE + 1}, format="json"
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
        response = self.client.patch(
            url, {"min_contributions": 3, "max_contributions": 5}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_an_evaluator_role_asks_for_at_most_the_bound(self):
        call = self.import_template("single_panel_review", offerings=0)
        self.client.force_authenticate(self.staff)
        url = factories.CallFactory.get_protected_url(call, "proposal_roles")
        for field in ("min_holders", "max_holders"):
            with self.subTest(field=field):
                response = self.client.post(
                    url,
                    {"name": f"Rapporteur {field}", field: models.MAX_STEP_PEOPLE + 1},
                    format="json",
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn(field, response.data)

    def test_an_import_is_held_to_the_bound(self):
        document = call_templates.load("single_panel_review")
        step = next(
            s
            for s in document["workflow_steps"]
            if s.get("contributor_source") == ContributorSources.ASSIGNED_REVIEWERS
        )
        step["min_contributions"] = models.MAX_STEP_PEOPLE + 1
        step["max_contributions"] = None
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {"manager": self.fixture.manager.uuid.hex, "call_data": document},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            f"less than or equal to {models.MAX_STEP_PEOPLE}", str(response.data)
        )


class TripwireCoverageTest(test.APITestCase):
    def listener(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        self.addCleanup(server.close)
        return server, server.getsockname()[1]

    def test_a_request_over_httpx_is_refused(self):
        server, port = self.listener()
        with tripwires.tripwires(), self.assertRaises(tripwires.DryRunSideEffect):
            httpx.get(f"http://127.0.0.1:{port}/leak")
        with self.assertRaises(TimeoutError):
            server.accept()

    def test_a_bare_connection_is_refused(self):
        server, port = self.listener()
        with tripwires.tripwires(), self.assertRaises(tripwires.DryRunSideEffect):
            socket.create_connection(("127.0.0.1", port), timeout=2)
        with self.assertRaises(TimeoutError):
            server.accept()
        # Unarmed, the same connection goes through.
        socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_mail_connections_tasks_and_publishing_are_refused(self):
        effects = {
            "core_utils.get_connection": lambda: core_utils.get_connection(),
            "send_task": lambda: current_app.send_task("anything"),
            "eager task": lambda: tasks.run_call_workflow_scenarios.apply(("x",)),
            "publish": lambda: kombu.Producer(mock.Mock()).publish({}),
        }
        for name, effect in effects.items():
            with self.subTest(effect=name):
                with (
                    tripwires.tripwires(),
                    self.assertRaises(tripwires.DryRunSideEffect),
                ):
                    effect()

    def test_a_thread_cannot_be_started_inside_a_run(self):
        results = {}

        def child():
            EmailMessage("s", "b", to=["x@example.com"]).send()
            results["sent"] = True

        with tripwires.tripwires(), self.assertRaises(tripwires.DryRunSideEffect):
            threading.Thread(target=child).start()
        self.assertEqual(results, {})
        self.assertEqual(mail.outbox, [])

    def test_a_thread_started_before_the_run_is_not_armed(self):
        ready, done = threading.Event(), threading.Event()
        results = {}

        def other():
            ready.wait(5)
            try:
                cache.set("other-thread", 1)
                results["ok"] = True
            except tripwires.DryRunSideEffect:
                results["ok"] = False
            done.set()

        thread = threading.Thread(target=other)
        thread.start()
        with tripwires.tripwires():
            ready.set()
            done.wait(5)
        thread.join(5)
        self.assertTrue(results["ok"])


class RoleCacheTest(test.APITestCase):
    def setUp(self):
        RoleManager.clear_cache()
        self.addCleanup(RoleManager.clear_cache)

    def test_a_role_made_inside_a_run_never_reaches_another_thread(self):
        name = "DRY_RUN.ONLY_ROLE"
        content_type = ContentType.objects.get_for_model(Customer)
        seen = {}
        armed, read = threading.Event(), threading.Event()

        def other():
            armed.wait(5)
            seen["cached"] = Role.objects._cache.get(name)
            read.set()

        thread = threading.Thread(target=other)
        thread.start()
        with tripwires.tripwires():
            role = Role.objects.get_system_role(name, content_type)
            again = Role.objects.get_system_role(name, content_type)
            self.assertEqual(again.pk, role.pk)
            armed.set()
            read.wait(5)
        thread.join(5)
        self.assertIsNone(seen["cached"])
        self.assertNotIn(name, Role.objects._cache)

    def test_a_role_created_is_cached_only_once_read_back(self):
        name = "DRY_RUN.NEW_ROLE"
        content_type = ContentType.objects.get_for_model(Customer)
        Role.objects.get_system_role(name, content_type)
        self.assertNotIn(name, Role.objects._cache)
        Role.objects.get_system_role(name, content_type)
        self.assertIn(name, Role.objects._cache)


class CallScenariosRunTest(ScenarioTestBase):
    def setUp(self):
        super().setUp()
        self.template = make_template(scenarios=[SCENARIO])
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {
                "manager": self.fixture.manager.uuid.hex,
                "template": self.template.uuid.hex,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.call = models.Call.objects.get(uuid=response.data["call_uuid"])
        factories.RequestedOfferingFactory(
            call=self.call,
            offering=marketplace_factories.OfferingFactory(),
            state=RequestedOfferingStates.ACCEPTED,
            created_by=self.fixture.owner,
        )

    def url(self, action):
        return factories.CallFactory.get_protected_url(self.call, action)

    def ask(self):
        with (
            mock.patch.object(tasks.run_call_workflow_scenarios, "delay") as delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(self.url("run-workflow-scenarios"))
        return response, delay

    def results(self):
        return self.client.get(self.url("workflow-scenarios")).data

    def test_a_run_on_its_way_is_not_asked_for_again(self):
        first, delay = self.ask()
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        second, again = self.ask()
        self.assertEqual(second.status_code, status.HTTP_202_ACCEPTED)
        delay.assert_called_once_with(self.call.uuid.hex)
        again.assert_not_called()
        self.assertEqual(second.data["requested_at"], first.data["requested_at"])
        tasks.run_call_workflow_scenarios(self.call.uuid.hex)
        self.assertEqual(self.results()["status"], "idle")

    def test_a_busy_scenario_leaves_the_earlier_results_in_place(self):
        self.ask()
        tasks.run_call_workflow_scenarios(self.call.uuid.hex)
        earlier = self.results()
        self.assertEqual(earlier["results"][0]["status"], "passed")

        self.ask()
        with mock.patch.object(dry_run, "run", side_effect=dry_run.DryRunBusy()):
            tasks.run_call_workflow_scenarios(self.call.uuid.hex)
        later = self.results()
        self.assertEqual(later["status"], "idle")
        self.assertEqual(later["results"], earlier["results"])
        self.assertEqual(later["checked_at"], earlier["checked_at"])
        self.assertEqual(later["error"], workflow_scenarios.BUSY_NOT_KEPT)

        # The next run that goes through clears the note.
        self.ask()
        tasks.run_call_workflow_scenarios(self.call.uuid.hex)
        self.assertIsNone(self.results()["error"])

    def test_a_batch_out_of_time_is_stopped_and_says_so(self):
        self.ask()
        with mock.patch.object(dry_run, "run", side_effect=SoftTimeLimitExceeded()):
            tasks.run_call_workflow_scenarios(self.call.uuid.hex)
        data = self.results()
        self.assertEqual(data["status"], "idle")
        self.assertEqual(data["results"], [])
        self.assertEqual(data["error"], workflow_scenarios.TOO_LONG_NOT_KEPT)

    def test_the_tasks_are_bounded_in_time(self):
        task = tasks.run_call_workflow_scenarios
        self.assertEqual(
            task.soft_time_limit, workflow_scenarios.CHECK_RUN_SOFT_LIMIT_SECONDS
        )
        self.assertGreater(task.time_limit, task.soft_time_limit)


class SyntheticRecipientsTest(DryRunMixin, test.APITestCase):
    def test_the_real_call_team_is_not_on_the_copy(self):
        call = self.import_template("single_panel_review", offerings=1)
        manager = structure_factories.UserFactory()
        call.add_user(manager, "CALL.MANAGER")
        holders = []
        original = dry_run._Run.copy_call

        def copy_call(run):
            copy = original(run)
            holders.extend(
                UserRole.objects.filter(
                    object_id=copy.pk,
                    content_type=ContentType.objects.get_for_model(models.Call),
                ).values_list("user_id", flat=True)
            )
            return copy

        with mock.patch.object(dry_run._Run, "copy_call", copy_call):
            self.ok(call)
        self.assertTrue(holders)
        self.assertNotIn(manager.pk, holders)
