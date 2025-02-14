from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.utils import send_mail
from waldur_core.logging import models
from waldur_core.structure.tests import fixtures as structure_fixtures

from .factories import EmailLogFactory


class EmailLogCreateTest(test.APITransactionTestCase):
    def test_create_log(self):
        send_mail(
            subject="notify user",
            body="notification text",
            to=["user_1@example.com", "user_2@example.com"],
            from_email="from@example.com",
        )
        self.assertTrue(
            models.EmailLog.objects.filter(
                subject="notify user",
                body="notification text",
                emails__contains=["user_1@example.com", "user_2@example.com"],
            ).exists()
        )


@ddt
class EmailLogViewTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.email_log = EmailLogFactory()
        self.url = EmailLogFactory.get_list_url()

    @data("staff", "global_support")
    def test_user_can_access_email_logs(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data))

    @data("owner", "user")
    def test_user_can_not_access_email_logs(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
