from ddt import ddt, data
from rest_framework import status

from waldur_mastermind.support.tests.base import override_support_settings
from . import factories, base


@ddt
class SupportUserRetreiveTest(base.BaseTest):

    def setUp(self):
        super(SupportUserRetreiveTest, self).setUp()
        self.support_user = factories.SupportUserFactory()

    @data('staff', 'global_support')
    def test_staff_or_support_can_retreive_support_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['uuid'], self.support_user.uuid.hex)

    @data('user')
    def test_user_can_not_retreive_support_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymouse_user_can_not_retreive_support_users(self):
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_support_settings(ENABLED=False)
    def test_user_can_not_retreive_support_users_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)
