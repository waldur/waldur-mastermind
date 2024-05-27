from ddt import data, ddt
from django.conf import settings
from rest_framework import status, test

from waldur_core.core import models as core_models
from waldur_core.core.authentication import TokenAuthentication
from waldur_core.logging import loggers
from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories, fixtures

IMPERSONATED_USER_HEADER = settings.WALDUR_CORE.get(
    "REQUEST_HEADER_IMPERSONATED_USER_UUID"
)
IMPERSONATOR_HEADER = settings.WALDUR_CORE.get("RESPONSE_HEADER_IMPERSONATOR_UUID")


@ddt
class ImpersonationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.impersonated_user = factories.UserFactory()

    @data("staff")
    def test_impersonation_is_available_for_user(self, user):
        impersonator = getattr(self.fixture, user)
        token = TokenAuthentication().get_model().objects.get(user=impersonator)
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": "Token " + token.key,
                IMPERSONATED_USER_HEADER: self.impersonated_user.uuid.hex,
            }
        )
        response = self.client.get("http://testserver/api/users/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.impersonated_user.uuid.hex)
        self.assertEqual(
            response.headers[IMPERSONATOR_HEADER.lower()], impersonator.uuid.hex
        )

    @data("user", "global_support")
    def test_impersonation_is_not_available_for_user(self, user):
        impersonator = getattr(self.fixture, user)
        token = TokenAuthentication().get_model().objects.get(user=impersonator)
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": "Token " + token.key,
                IMPERSONATED_USER_HEADER: self.impersonated_user.uuid.hex,
            }
        )
        response = self.client.get("http://testserver/api/users/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response.data["uuid"], self.impersonated_user.uuid.hex)
        self.assertEqual(response.data["uuid"], impersonator.uuid.hex)

    def test_event_logs(self):
        user = core_models.ImpersonatedUser.objects.get(
            uuid=self.impersonated_user.uuid.hex
        )
        user.impersonator = self.fixture.staff

        class TestLogger(loggers.EventLogger):
            user = core_models.User

            class Meta:
                event_types = ("test_event",)

        loggers.event_logger.register("test_event", TestLogger)
        loggers.event_logger.test_event.info(
            "Test",
            event_type="test_event",
            event_context={
                "user": user,
            },
        )

        event_log = logging_models.Event.objects.get(event_type="test_event")
        self.assertEqual(
            event_log.context["user_impersonator_uuid"], self.fixture.staff.uuid.hex
        )
