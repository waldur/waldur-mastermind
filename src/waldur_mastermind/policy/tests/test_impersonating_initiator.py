"""Who stood behind an impersonation stays with staff and support.

A policy evaluation records the person who started it. Under impersonation
that is a staff member acting as someone else, whom the event's other fields
hide from every other reader; the initiator fields must not name them either,
in the context or in the message.
"""

from rest_framework import test

from waldur_core.core import models as core_models
from waldur_core.core.utils import get_system_robot
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.middleware import reset_event_context, set_event_context
from waldur_core.logging.models import Event, Feed
from waldur_core.server import celeryconf
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.policy import utils
from waldur_mastermind.policy.structures import EvaluationTriggers


class ImpersonatingInitiatorTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.scope_url = structure_factories.ProjectFactory.get_url(
            self.fixture.project
        )

    def tearDown(self):
        reset_event_context()
        super().tearDown()

    def impersonate(self):
        impersonated = core_models.ImpersonatedUser.all_objects.get(pk=self.owner.pk)
        impersonated.impersonator = self.staff
        set_event_context(impersonated._get_log_context("user"))

    def emit_in_evaluation(self, template="The policy paused a resource."):
        with utils.system_actor_event_context(EvaluationTriggers.CREDIT_CHANGED):
            event_logger.emit(
                template,
                EventType.PROJECT_UPDATE_SUCCEEDED,
                {"project": self.fixture.project},
                scopes=[self.fixture.project],
            )
        reset_event_context()

    def read_as(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            "/api/events/",
            {
                "scope": self.scope_url,
                "event_type": EventType.PROJECT_UPDATE_SUCCEEDED,
            },
        )
        self.assertEqual(response.status_code, 200)
        [event] = response.data
        return event

    def assertNamesNoImpersonator(self, data):
        text = repr(data)
        self.assertNotIn(self.staff.username, text)
        self.assertNotIn(self.staff.uuid.hex, text)

    def test_others_see_the_account_acted_as(self):
        self.impersonate()
        self.emit_in_evaluation()

        context = self.read_as(self.fixture.admin)["context"]

        self.assertEqual(context["initiated_by_username"], self.owner.username)
        self.assertEqual(context["initiated_by_uuid"], self.owner.uuid.hex)
        self.assertNamesNoImpersonator(context)

    def test_staff_see_the_impersonator(self):
        self.impersonate()
        self.emit_in_evaluation()

        context = self.read_as(self.staff)["context"]

        self.assertEqual(
            context["initiated_by_impersonator_username"], self.staff.username
        )
        self.assertEqual(context["initiated_by_impersonator_uuid"], self.staff.uuid.hex)
        self.assertEqual(context["initiated_by_username"], self.owner.username)

    def test_a_placeholder_in_a_name_does_not_name_the_impersonator(self):
        # Names people write reach templates; a planted placeholder must not
        # pull the impersonator into the message.
        self.impersonate()
        self.emit_in_evaluation(
            "Resource {initiated_by_username} {initiated_by_impersonator_username} "
            "{initiated_by_uuid} {initiated_by_impersonator_uuid} was paused."
        )

        event = self.read_as(self.fixture.admin)

        self.assertNamesNoImpersonator(event["message"])

    def test_an_event_stored_before_the_change_is_shown_the_same_way(self):
        event = Event.objects.create(
            event_type=EventType.PROJECT_UPDATE_SUCCEEDED,
            message="The policy paused a resource.",
            context={
                "user_username": "system_robot",
                "trigger": EvaluationTriggers.CREDIT_CHANGED,
                "initiated_by_uuid": self.staff.uuid.hex,
                "initiated_by_username": self.staff.username,
                "initiated_as_username": self.owner.username,
            },
        )
        Feed.objects.create(scope=self.fixture.project, event=event)

        others = self.read_as(self.fixture.admin)["context"]
        staff = self.read_as(self.staff)["context"]

        self.assertEqual(others["initiated_by_username"], self.owner.username)
        self.assertNotIn("initiated_by_uuid", others)
        self.assertNotIn("initiated_as_username", others)
        self.assertNamesNoImpersonator(others)
        self.assertEqual(staff["initiated_by_username"], self.staff.username)
        self.assertEqual(staff["initiated_as_username"], self.owner.username)

    def test_without_impersonation_the_initiator_is_shown_to_everyone(self):
        set_event_context(self.owner._get_log_context("user"))
        self.emit_in_evaluation()

        context = self.read_as(self.fixture.admin)["context"]

        self.assertEqual(context["initiated_by_username"], self.owner.username)
        self.assertEqual(context["initiated_by_uuid"], self.owner.uuid.hex)

    def carried_by_a_task_queued_earlier(self):
        """Bind what a task queued by an earlier version carries: the robot as
        the user, and the initiator fields in the old shape."""
        celeryconf.bind_event_context(
            kwargs={
                "event_context": {
                    **get_system_robot()._get_log_context("user"),
                    "trigger": EvaluationTriggers.CREDIT_CHANGED,
                    "initiated_by_uuid": self.staff.uuid.hex,
                    "initiated_by_username": self.staff.username,
                    "initiated_as_username": self.owner.username,
                }
            }
        )

    def test_an_earlier_shape_carried_by_a_task_keeps_both_through_nested_blocks(
        self,
    ):
        self.carried_by_a_task_queued_earlier()
        with utils.system_actor_event_context(EvaluationTriggers.CREDIT_CHANGED):
            self.emit_in_evaluation()

        staff = self.read_as(self.staff)["context"]
        others = self.read_as(self.fixture.admin)["context"]

        self.assertEqual(staff["initiated_by_username"], self.owner.username)
        self.assertEqual(
            staff["initiated_by_impersonator_username"], self.staff.username
        )
        self.assertEqual(staff["initiated_by_impersonator_uuid"], self.staff.uuid.hex)
        self.assertNotIn("initiated_as_username", staff)
        self.assertNamesNoImpersonator(others)

    def test_an_earlier_shape_carried_by_a_task_fills_no_placeholder(self):
        self.carried_by_a_task_queued_earlier()
        event_logger.emit(
            "Resource {initiated_by_username} {initiated_by_uuid} was paused.",
            EventType.PROJECT_UPDATE_SUCCEEDED,
            {"project": self.fixture.project},
            scopes=[self.fixture.project],
        )
        reset_event_context()

        self.assertNamesNoImpersonator(self.read_as(self.fixture.admin)["message"])
