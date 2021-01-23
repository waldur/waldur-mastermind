from unittest import mock

from rest_framework import test

from waldur_jira import models
from waldur_jira.backend import JiraBackend
from waldur_jira.tests import factories, fixtures


class PriorityPullTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.JiraFixture()

        self.highest_priority = mock.MagicMock(
            id='10',
            description='This problem will block progress.',
            iconUrl='http://example.com/highest.svg',
        )
        self.highest_priority.name = 'Highest'

        self.lowest_priority = mock.Mock(
            id='100',
            description='Trivial problem with little or no impact on progress.',
            iconUrl='http://example.com/lowest.svg',
        )
        self.lowest_priority.name = 'Lowest'
        self.priorities = [self.highest_priority, self.lowest_priority]

        self.backend = JiraBackend(self.fixture.service_settings)
        self.backend.manager = mock.Mock()
        self.backend.manager.priorities.return_value = self.priorities

    def test_create_new_priorities(self):
        self.backend.pull_priorities()
        self.assert_priorities_are_pulled()

    def test_update_existing_priority(self):
        factories.PriorityFactory(
            settings=self.fixture.service_settings,
            backend_id=self.highest_priority.id,
            name='Old name',
        )
        self.backend.pull_priorities()
        self.assert_priorities_are_pulled()

    def test_delete_stale_priorities(self):
        self.backend.manager.priorities.return_value = []
        self.backend.pull_priorities()
        self.assertEqual(models.Priority.objects.count(), 0)

    def assert_priorities_are_pulled(self):
        for priority in self.priorities:
            self.assertTrue(
                models.Priority.objects.filter(
                    settings=self.fixture.service_settings,
                    backend_id=priority.id,
                    name=priority.name,
                    description=priority.description,
                    icon_url=priority.iconUrl,
                ).exists()
            )
