from unittest import mock

from django.test import TestCase, override_settings
from rest_framework import status, test

from waldur_core.structure import utils as structure_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_site_agent import utils
from waldur_mastermind.marketplace_site_agent.tests import (
    fixtures as site_agent_fixtures,
)


class UnlinkTest(test.APITestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        self.url = factories.ResourceFactory.get_url(
            self.fixture.resource, action="unlink"
        )

    def test_unlink(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)

    def test_unlink_erred_resource(self):
        self.client.force_authenticate(self.fixture.staff)
        self.fixture.resource.state = ResourceStates.ERRED
        self.fixture.resource.save()
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)


class ResourcePullTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        resource = self.fixture.resource
        resource.state = ResourceStates.OK
        resource.backend_id = "test-backend-id"
        resource.scope = None
        resource.save()
        self.url = factories.ResourceFactory.get_url(
            self.fixture.resource, action="pull"
        )

    @override_settings(task_always_eager=True)
    @mock.patch(
        "waldur_mastermind.marketplace_site_agent.utils.push_resource_update_message"
    )
    def test_resource_pull(self, mock_push_resource_update_message):
        mock_push_resource_update_message.return_value = None
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_202_ACCEPTED, response.status_code, response.data)
        mock_push_resource_update_message.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch(
        "waldur_mastermind.marketplace_site_agent.utils.push_resource_update_message"
    )
    def test_resource_pull_passes_force_true(self, mock_push_resource_update_message):
        """User-triggered pull must pass force=True to bypass deduplication."""
        mock_push_resource_update_message.return_value = None
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.url)
        mock_push_resource_update_message.assert_called_once_with(mock.ANY, force=True)


class PushResourceUpdateMessageTest(TestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.backend_id = "test-backend-id"
        self.resource.save()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    @mock.patch(
        "waldur_mastermind.marketplace.utils.prepare_messages",
        return_value=[{"vhost": "v", "topic": "t", "payload": "p"}],
    )
    def test_force_sends_even_when_content_unchanged(self, mock_prepare, mock_publish):
        """With force=True, message is sent even if payload hash hasn't changed."""
        utils.push_resource_update_message(self.resource, force=True)
        self.assertEqual(mock_publish.call_count, 1)

        # Second call with identical content — still sent because force=True
        utils.push_resource_update_message(self.resource, force=True)
        self.assertEqual(mock_publish.call_count, 2)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    @mock.patch(
        "waldur_mastermind.marketplace.utils.prepare_messages",
        return_value=[{"vhost": "v", "topic": "t", "payload": "p"}],
    )
    def test_periodic_skips_when_content_unchanged(self, mock_prepare, mock_publish):
        """Without force, second call with same content is skipped (periodic dedup)."""
        utils.push_resource_update_message(self.resource, force=False)
        self.assertEqual(mock_publish.call_count, 1)

        # Second call with identical content — skipped
        utils.push_resource_update_message(self.resource, force=False)
        self.assertEqual(mock_publish.call_count, 1)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    @mock.patch(
        "waldur_mastermind.marketplace.utils.prepare_messages",
        return_value=[{"vhost": "v", "topic": "t", "payload": "p"}],
    )
    def test_force_updates_cache_for_periodic(self, mock_prepare, mock_publish):
        """force=True updates the cache, so a subsequent periodic call is skipped."""
        utils.push_resource_update_message(self.resource, force=True)
        self.assertEqual(mock_publish.call_count, 1)

        # Periodic call sees same content in cache — skipped
        utils.push_resource_update_message(self.resource, force=False)
        self.assertEqual(mock_publish.call_count, 1)


class ProjectMoveResourceMessageTest(test.APITestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        resource = self.fixture.resource
        resource.state = ResourceStates.OK
        resource.save()
        self.new_customer = structure_factories.CustomerFactory()

    @mock.patch(
        "waldur_mastermind.marketplace_site_agent.utils.push_resource_update_message"
    )
    def test_resource_message_sent_on_project_move(self, mock_push):
        structure_utils.move_project(
            self.fixture.project, self.new_customer, preserve_permissions=True
        )
        mock_push.assert_called_once_with(self.fixture.resource, force=True)

    @mock.patch(
        "waldur_mastermind.marketplace_site_agent.utils.push_resource_update_message"
    )
    def test_non_ok_resource_not_notified_on_project_move(self, mock_push):
        self.fixture.resource.state = ResourceStates.ERRED
        self.fixture.resource.save()
        structure_utils.move_project(
            self.fixture.project, self.new_customer, preserve_permissions=True
        )
        mock_push.assert_not_called()
