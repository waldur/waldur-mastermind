from unittest import mock

from django.test import TestCase

from waldur_auth_valimo import client, models, tasks
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories


@mock.patch("waldur_auth_valimo.tasks.client.StatusRequest.execute")
class ValimoLoginEventTest(TestCase):
    def setUp(self):
        self.auth_result = models.AuthResult.objects.create(
            phone="+3725555555", state=models.AuthResult.States.PROCESSING
        )

    def poll(self, execute, **response):
        execute.return_value = mock.Mock(**response)
        tasks.PollTask().execute(self.auth_result)

    def test_successful_login_is_logged(self, execute):
        user = structure_factories.UserFactory(civil_number="EE123")

        self.poll(execute, status=client.Statuses.OK, civil_number="EE123")

        event = Event.objects.get(event_type=EventType.AUTH_LOGGED_IN_WITH_VALIMO)
        self.assertEqual(event.context["user_uuid"], user.uuid.hex)

    def test_login_of_unknown_user_is_logged(self, execute):
        self.poll(execute, status=client.Statuses.OK, civil_number="EE404")

        event = Event.objects.get(event_type=EventType.AUTH_LOGIN_FAILED_WITH_VALIMO)
        self.assertEqual(event.context["reason"], "User is not registered.")

    def test_login_rejected_by_backend_is_logged(self, execute):
        self.poll(execute, status=client.Statuses.ERRED, details="User cancel.")

        event = Event.objects.get(event_type=EventType.AUTH_LOGIN_FAILED_WITH_VALIMO)
        self.assertEqual(event.context["reason"], "User cancel.")
        self.assertNotIn("+3725555555", event.message)


class ValimoErredLoginEventTest(TestCase):
    def test_erred_login_is_logged(self):
        auth_result = models.AuthResult.objects.create(
            phone="+3725555555", state=models.AuthResult.States.PROCESSING
        )
        task = tasks.AuthErrorTask()
        task.result = mock.Mock(result="Max retries exceeded", traceback="")

        task.execute(auth_result)

        event = Event.objects.get(event_type=EventType.AUTH_LOGIN_FAILED_WITH_VALIMO)
        self.assertEqual(event.context["reason"], "Max retries exceeded")
