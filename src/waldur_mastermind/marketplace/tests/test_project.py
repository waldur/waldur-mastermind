import datetime
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import move_project
from waldur_mastermind.marketplace import models, tasks
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class RemovalOfExpiredProjectWithoutActiveResourcesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource_1 = self.fixture.resource
        self.resource_1.state = ResourceStates.OK
        self.resource_1.save()
        self.resource_2 = models.Resource.objects.create(
            project=self.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            state=ResourceStates.OK,
        )
        self.project.end_date = datetime.datetime(year=2020, month=1, day=1).date()
        self.project.save()

    def project_exists(self):
        return structure_models.Project.available_objects.filter(
            id=self.project.id
        ).exists()

    def deletion_events(self):
        return Event.objects.filter(event_type=EventType.PROJECT_DELETION_TRIGGERED)

    def test_delete_expired_project_if_every_resource_has_been_terminated(self):
        with freeze_time("2020-01-01"):
            self.assertTrue(self.project.is_expired)
            with mock.patch.object(
                tasks.delete_expired_project,
                "delay",
                side_effect=tasks.delete_expired_project,
            ):
                self.resource_1.state = ResourceStates.TERMINATED
                with self.captureOnCommitCallbacks(execute=True):
                    self.resource_1.save()
                self.assertTrue(self.project_exists())
                self.resource_2.state = ResourceStates.TERMINATED
                with self.captureOnCommitCallbacks(execute=True):
                    self.resource_2.save()
                self.assertFalse(self.project_exists())

    def test_deletion_is_scheduled_after_commit_not_run_in_request(self):
        with freeze_time("2020-01-01"):
            models.Resource.objects.filter(id=self.resource_1.id).update(
                state=ResourceStates.TERMINATED
            )
            with mock.patch.object(tasks.delete_expired_project, "delay") as mock_delay:
                self.resource_2.state = ResourceStates.TERMINATED
                with self.captureOnCommitCallbacks(execute=True):
                    self.resource_2.save()
                mock_delay.assert_called_once_with(self.project.uuid.hex)
            self.assertTrue(self.project_exists())

    def test_task_is_not_scheduled_when_project_is_not_expired(self):
        with freeze_time("2019-12-01"):
            models.Resource.objects.filter(id=self.resource_1.id).update(
                state=ResourceStates.TERMINATED
            )
            with mock.patch.object(tasks.delete_expired_project, "delay") as mock_delay:
                self.resource_2.state = ResourceStates.TERMINATED
                with self.captureOnCommitCallbacks(execute=True):
                    self.resource_2.save()
                mock_delay.assert_not_called()

    def test_task_deletes_expired_project_without_active_resources(self):
        with freeze_time("2020-01-01"):
            models.Resource.objects.update(state=ResourceStates.TERMINATED)
            tasks.delete_expired_project(self.project.uuid.hex)
            self.assertFalse(self.project_exists())

    def test_task_emits_deletion_triggered_event(self):
        with freeze_time("2020-01-01"):
            models.Resource.objects.update(state=ResourceStates.TERMINATED)
            tasks.delete_expired_project(self.project.uuid.hex)
            event = self.deletion_events().order_by("-created").first()
            self.assertIsNotNone(event)
            self.assertIn(self.project.name, event.message)
            self.assertEqual(event.context["project_uuid"], self.project.uuid.hex)

    def test_task_does_not_emit_event_when_deletion_is_skipped(self):
        with freeze_time("2020-01-01"):
            models.Resource.objects.filter(id=self.resource_1.id).update(
                state=ResourceStates.TERMINATED
            )
            tasks.delete_expired_project(self.project.uuid.hex)
            self.assertFalse(self.deletion_events().exists())

    def test_task_skips_project_that_is_no_longer_expired(self):
        with freeze_time("2020-01-01"):
            models.Resource.objects.update(state=ResourceStates.TERMINATED)
            self.project.end_date = None
            self.project.save()
            tasks.delete_expired_project(self.project.uuid.hex)
            self.assertTrue(self.project_exists())

    def test_task_skips_project_with_active_resources(self):
        with freeze_time("2020-01-01"):
            models.Resource.objects.filter(id=self.resource_1.id).update(
                state=ResourceStates.TERMINATED
            )
            tasks.delete_expired_project(self.project.uuid.hex)
            self.assertTrue(self.project_exists())

    def test_task_ignores_unknown_project(self):
        tasks.delete_expired_project("0" * 32)


class MarketplaceResourceCountTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

    def test_key_marketplace_resource_count_exists_in_project_response(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = structure_factories.ProjectFactory.get_url(self.fixture.resource.project)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        counters = response.json()["marketplace_resource_count"]
        self.assertEqual(
            counters[self.resource.offering.category.uuid.hex],
            1,
        )


class ProjectMoveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.project = self.fixture.offering.project
        self.old_customer = self.project.customer
        self.new_customer = structure_factories.CustomerFactory()

    def change_customer(self):
        move_project(self.project, self.new_customer)
        self.project.refresh_from_db()

    def test_change_customer(self):
        self.change_customer()
        self.assertEqual(self.new_customer, self.project.customer)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.new_customer)

    def test_change_customer_if_offering_scope_is_resource(self):
        resource = factories.ResourceFactory(project=self.project)
        self.offering.scope = resource
        self.offering.save()

        self.change_customer()
        self.assertEqual(self.new_customer, self.project.customer)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.new_customer)

        resource.refresh_from_db()
        self.assertEqual(resource.customer, self.new_customer)

    def test_change_customer_for_private_offering(self):
        private_offering = factories.OfferingFactory(
            project=self.project,
            customer=self.old_customer,
            shared=False,
        )
        self.change_customer()
        self.assertEqual(self.new_customer, self.project.customer)
        private_offering.refresh_from_db()
        self.assertEqual(private_offering.customer, self.new_customer)


class ProjectStartDateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.start_date = datetime.date.today()
        self.project.save()
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROJECT
        self.order.save()

    def test_order_process_when_project_start_date_unset(self):
        # Arrange
        # Mocking to simulate an auto-approving offering
        with mock.patch(
            "waldur_mastermind.marketplace.utils.order_should_not_be_reviewed_by_provider"
        ) as mocked_check:
            mocked_check.return_value = True
            # Act
            self.project.start_date = None
            self.project.save()

        # Assert: The signal handler should have moved the order to EXECUTING
        self.order.refresh_from_db()
        self.assertEqual(OrderStates.EXECUTING, self.order.state)

    @override_settings(task_always_eager=True)
    def test_order_process_when_project_started_and_no_reviews_needed(self):
        with mock.patch(
            "waldur_mastermind.marketplace.utils.order_should_not_be_reviewed_by_provider"
        ) as order_should_not_be_reviewed_by_provider_mock:
            order_should_not_be_reviewed_by_provider_mock.return_value = True
            tasks.process_pending_project_orders()

        self.order.refresh_from_db()
        self.order.resource.refresh_from_db()

        self.assertEqual(OrderStates.DONE, self.order.state)
        self.assertEqual(ResourceStates.OK, self.order.resource.state)

    def test_order_moves_to_pending_start_date_when_project_starts(self):
        # Arrange: The project is ready, but the order has its own future start date.
        self.order.start_date = timezone.now().date() + datetime.timedelta(days=5)
        self.order.save()

        with mock.patch(
            "waldur_mastermind.marketplace.utils.order_should_not_be_reviewed_by_provider"
        ) as mocked_check:
            # Simulate an offering that does NOT require provider review
            mocked_check.return_value = True

            # Act: Run the task that processes projects that have just started
            tasks.process_pending_project_orders()

        # Assert: The order should now be waiting for its own start date.
        self.order.refresh_from_db()
        self.assertEqual(OrderStates.PENDING_START_DATE, self.order.state)

    def test_order_moves_to_pending_provider_when_project_starts(self):
        # Arrange: The project is ready, but the offering requires provider review.
        with mock.patch(
            "waldur_mastermind.marketplace.utils.order_should_not_be_reviewed_by_provider"
        ) as mocked_check:
            # Simulate an offering that REQUIRES provider review
            mocked_check.return_value = False

            # Act: Run the task
            tasks.process_pending_project_orders()

        # Assert: The order should have moved to the next step in the approval chain.
        self.order.refresh_from_db()
        self.assertEqual(OrderStates.PENDING_PROVIDER, self.order.state)


@override_settings(task_always_eager=True)
class OrderStartDateTaskTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_START_DATE
        self.order.save()

    @freeze_time("2024-01-15")
    def test_order_is_processed_when_start_date_is_reached(self):
        # Arrange: Set the order's start date to today
        self.order.start_date = timezone.now().date()
        self.order.save()

        # Act
        tasks.process_pending_start_date_orders()

        # Assert
        self.order.refresh_from_db()
        self.order.resource.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertEqual(self.order.resource.state, ResourceStates.OK)

    @freeze_time("2024-01-15")
    def test_order_is_processed_when_start_date_is_in_the_past(self):
        # Arrange: Set the order's start date to a past date
        self.order.start_date = timezone.now().date() - datetime.timedelta(days=5)
        self.order.save()

        # Act
        tasks.process_pending_start_date_orders()

        # Assert
        self.order.refresh_from_db()
        self.order.resource.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertEqual(self.order.resource.state, ResourceStates.OK)

    @freeze_time("2024-01-15")
    def test_order_is_not_processed_if_start_date_is_in_future(self):
        # Arrange: Set the order's start date to a future date
        self.order.start_date = timezone.now().date() + datetime.timedelta(days=1)
        self.order.save()

        # Act
        tasks.process_pending_start_date_orders()

        # Assert
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.PENDING_START_DATE)

    @freeze_time("2024-01-15")
    def test_order_in_wrong_state_is_not_processed(self):
        # Arrange: Set the order to a different state but with a past start date
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.start_date = timezone.now().date() - datetime.timedelta(days=1)
        self.order.save()

        # Act
        tasks.process_pending_start_date_orders()

        # Assert
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.PENDING_PROVIDER)
