from unittest import mock

from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class UnlinkTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.scope = structure_factories.TestNewInstanceFactory()
        self.resource.scope = self.scope
        self.resource.state = ResourceStates.ERRED
        self.resource.save()

    def unlinked_events(self):
        return logging_models.Event.objects.filter(
            event_type=EventType.MARKETPLACE_RESOURCE_UNLINKED
        )

    def assert_nothing_deleted(self):
        self.assertTrue(models.Resource.objects.filter(id=self.resource.id).exists())
        self.assertTrue(type(self.scope).objects.filter(id=self.scope.id).exists())


class UnlinkViewTest(UnlinkTestBase):
    def setUp(self):
        super().setUp()
        self.url = factories.ResourceFactory.get_url(self.resource, action="unlink")
        self.client.force_authenticate(self.fixture.staff)

    @mock.patch.object(tasks.unlink_resource, "delay")
    def test_schedules_task_and_changes_nothing(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url)

        self.assertEqual(status.HTTP_202_ACCEPTED, response.status_code)
        delay.assert_called_once_with(self.resource.uuid.hex)
        self.assert_nothing_deleted()
        self.resource.refresh_from_db()
        self.assertEqual(ResourceStates.ERRED, self.resource.state)
        self.assertFalse(self.unlinked_events().exists())


class UnlinkTaskTest(UnlinkTestBase):
    def run_task(self):
        tasks.unlink_resource(self.resource.uuid.hex)

    def test_deletes_resource_and_scope(self):
        self.run_task()

        self.assertFalse(models.Resource.objects.filter(id=self.resource.id).exists())
        self.assertFalse(type(self.scope).objects.filter(id=self.scope.id).exists())

    def test_records_one_event_on_project_and_customer_feeds(self):
        self.run_task()

        event = self.unlinked_events().get()
        feeds = logging_models.Feed.objects.filter(event=event)
        self.assertEqual(
            {self.resource.project, self.resource.project.customer},
            {feed.scope for feed in feeds if feed.scope},
        )

    def test_repeated_unlink_does_nothing(self):
        self.run_task()
        self.run_task()

        self.assertEqual(1, self.unlinked_events().count())

    def test_missing_resource_does_nothing(self):
        tasks.unlink_resource("0" * 32)

        self.assert_nothing_deleted()
        self.assertFalse(self.unlinked_events().exists())

    def test_failed_resource_delete_rolls_back_the_scope_delete(self):
        with mock.patch.object(
            models.Resource, "delete", side_effect=ValueError("Resource delete failed")
        ):
            with self.assertRaisesMessage(ValueError, "Resource delete failed"):
                self.run_task()

        self.assert_nothing_deleted()
        self.resource.refresh_from_db()
        self.assertEqual(ResourceStates.ERRED, self.resource.state)
        self.assertEqual("Resource delete failed", self.resource.error_message)
        self.assertIn("ValueError", self.resource.error_traceback)
        self.assertFalse(self.unlinked_events().exists())
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED
            ).exists()
        )

    def test_failed_scope_delete_marks_ok_resource_as_erred(self):
        self.resource.state = ResourceStates.OK
        self.resource.save()

        with mock.patch.object(
            type(self.scope), "delete", side_effect=ValueError("Scope delete failed")
        ):
            with self.assertRaisesMessage(ValueError, "Scope delete failed"):
                self.run_task()

        self.assert_nothing_deleted()
        self.resource.refresh_from_db()
        self.assertEqual(ResourceStates.ERRED, self.resource.state)
        self.assertEqual("Scope delete failed", self.resource.error_message)
        self.assertFalse(self.unlinked_events().exists())

    def test_failed_unlink_keeps_terminated_state(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()

        with mock.patch.object(
            models.Resource, "delete", side_effect=ValueError("Resource delete failed")
        ):
            with self.assertRaises(ValueError):
                self.run_task()

        self.resource.refresh_from_db()
        self.assertEqual(ResourceStates.TERMINATED, self.resource.state)
        self.assertEqual("Resource delete failed", self.resource.error_message)
