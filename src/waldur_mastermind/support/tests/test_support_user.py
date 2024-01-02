from unittest import mock

import jira
from constance.test.pytest import override_config
from ddt import data, ddt
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import exceptions, tasks
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.tests import base, factories


@ddt
class SupportUserRetrieveTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.SupportUserFactory()

    @data('staff', 'global_support')
    def test_staff_or_support_can_retrieve_support_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['uuid'], self.support_user.uuid.hex)

    @data('user')
    def test_user_can_not_retrieve_support_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymouse_user_can_not_retrieve_support_users(self):
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_user_can_not_retrieve_support_users_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)


@override_config(WALDUR_SUPPORT_ENABLED=True)
class SupportUserPullTest(base.BaseTest):
    def setUp(self):
        mock_patch = mock.patch('waldur_jira.backend.JIRA')
        self.mocked_jira = mock_patch.start()

        class AtlassianUser:
            displayName = 'alice'
            name = 'alice'
            key = 'alice'
            accountId = 'alice'

        self.mocked_jira().search_assignable_users_for_projects.return_value = [
            AtlassianUser()
        ]

    def tearDown(self):
        mock.patch.stopall()

    def test_if_user_is_not_available_he_is_marked_as_disabled(self):
        # Arrange
        alice = factories.SupportUserFactory(
            backend_id='alice', backend_name='atlassian'
        )
        bob = factories.SupportUserFactory(backend_id='bob', backend_name='atlassian')

        # Act
        ServiceDeskBackend().pull_support_users()

        # Assert
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertTrue(alice.is_active)
        self.assertFalse(bob.is_active)

    def test_if_user_is_available_he_is_marked_as_enabled(self):
        # Arrange
        alice = factories.SupportUserFactory(
            backend_id='alice', is_active=False, backend_name='atlassian'
        )

        # Act
        tasks.pull_support_users()

        # Assert
        alice.refresh_from_db()
        self.assertTrue(alice.is_active)


@override_config(WALDUR_SUPPORT_ENABLED=True)
@mock.patch('waldur_jira.backend.JIRA')
class SupportUserValidateTest(base.BaseTest):
    def test_not_create_user_if_user_exists_but_he_inactive(self, mocked_jira):
        def side_effect(*args, **kwargs):
            if kwargs.get('includeInactive'):
                return [jira.User(None, None, raw={'active': False})]
            return []

        mocked_jira().search_users.side_effect = side_effect
        user = structure_factories.UserFactory()
        self.assertRaises(
            exceptions.SupportUserInactive, ServiceDeskBackend().create_user, user
        )
