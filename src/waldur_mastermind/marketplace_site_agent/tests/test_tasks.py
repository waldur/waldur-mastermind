import datetime
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone

from waldur_mastermind.marketplace_site_agent import enums, tasks
from waldur_mastermind.marketplace_site_agent.tests import factories


class MarkAgentServicesAsInactiveTest(TransactionTestCase):
    def setUp(self):
        self.agent_identity = factories.AgentIdentityFactory()
        self.agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            state=enums.AgentServiceState.ACTIVE,
        )
        self.processor = factories.AgentProcessorFactory(service=self.agent_service)

    def test_agent_service_marked_as_idle_when_processor_inactive_over_10_minutes(self):
        """Test that active agent service is marked as IDLE when processor hasn't run for over 10 minutes."""
        # Arrange - set processor last_run to 11 minutes ago
        eleven_minutes_ago = timezone.now() - datetime.timedelta(minutes=11)
        self.processor.last_run = eleven_minutes_ago
        self.processor.save()

        # Act
        tasks.mark_agent_services_as_inactive()

        # Assert
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.IDLE)

    def test_agent_service_remains_active_when_processor_recently_active(self):
        """Test that active agent service remains ACTIVE when processor ran within last 10 minutes."""
        # Arrange - set processor last_run to 5 minutes ago
        five_minutes_ago = timezone.now() - datetime.timedelta(minutes=5)
        self.processor.last_run = five_minutes_ago
        self.processor.save()

        tasks.mark_agent_services_as_inactive()

        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.ACTIVE)

    def test_agent_service_marked_idle_at_exactly_10_minutes(self):
        """Test that agent service is marked IDLE when processor last ran exactly 10 minutes ago.

        Note: The task uses <= comparison, so exactly 10 minutes triggers the transition.
        """
        # Arrange - set processor last_run to exactly 10 minutes ago
        ten_minutes_ago = timezone.now() - datetime.timedelta(minutes=10)
        self.processor.last_run = ten_minutes_ago
        self.processor.save()

        tasks.mark_agent_services_as_inactive()

        # Assert - marked as IDLE due to <= comparison
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.IDLE)

    def test_multiple_processors_uses_latest_last_run(self):
        """Test that when service has multiple processors, the most recent last_run is used."""
        # Arrange - create additional processors
        old_processor = factories.AgentProcessorFactory(service=self.agent_service)
        old_processor.last_run = timezone.now() - datetime.timedelta(minutes=20)
        old_processor.save()

        recent_processor = factories.AgentProcessorFactory(service=self.agent_service)
        recent_processor.last_run = timezone.now() - datetime.timedelta(minutes=5)
        recent_processor.save()

        tasks.mark_agent_services_as_inactive()

        # Assert - service should remain active because of the recent processor
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.ACTIVE)

    def test_multiple_processors_all_inactive_marks_service_idle(self):
        """Test that when all processors are inactive over 10 minutes, service is marked IDLE."""
        # Arrange - set all processors to inactive
        self.processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        self.processor.save()

        processor2 = factories.AgentProcessorFactory(service=self.agent_service)
        processor2.last_run = timezone.now() - datetime.timedelta(minutes=12)
        processor2.save()

        tasks.mark_agent_services_as_inactive()

        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.IDLE)

    def test_only_active_services_are_checked(self):
        """Test that only ACTIVE agent services are evaluated for inactivity."""
        # Arrange - create services in different states
        idle_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            state=enums.AgentServiceState.IDLE,
        )
        idle_processor = factories.AgentProcessorFactory(service=idle_service)
        idle_processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        idle_processor.save()

        error_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            state=enums.AgentServiceState.ERROR,
        )
        error_processor = factories.AgentProcessorFactory(service=error_service)
        error_processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        error_processor.save()

        tasks.mark_agent_services_as_inactive()

        # Assert - IDLE and ERROR services should remain unchanged
        idle_service.refresh_from_db()
        self.assertEqual(idle_service.state, enums.AgentServiceState.IDLE)

        error_service.refresh_from_db()
        self.assertEqual(error_service.state, enums.AgentServiceState.ERROR)

    def test_multiple_active_services_processed_independently(self):
        """Test that multiple active services are processed independently."""
        # Arrange - create two active services with different processor states
        active_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            state=enums.AgentServiceState.ACTIVE,
        )
        active_processor = factories.AgentProcessorFactory(service=active_service)
        active_processor.last_run = timezone.now() - datetime.timedelta(minutes=5)
        active_processor.save()

        inactive_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            state=enums.AgentServiceState.ACTIVE,
        )
        inactive_processor = factories.AgentProcessorFactory(service=inactive_service)
        inactive_processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        inactive_processor.save()

        tasks.mark_agent_services_as_inactive()

        active_service.refresh_from_db()
        self.assertEqual(active_service.state, enums.AgentServiceState.ACTIVE)

        inactive_service.refresh_from_db()
        self.assertEqual(inactive_service.state, enums.AgentServiceState.IDLE)

    @mock.patch("waldur_mastermind.marketplace_site_agent.tasks.logger")
    def test_logging_when_service_marked_inactive(self, mock_logger):
        """Test that appropriate log message is generated when service is marked as inactive."""
        self.processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        self.processor.save()

        tasks.mark_agent_services_as_inactive()

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        self.assertIn(self.agent_service.name, call_args[0][1])
        self.assertIn("marked as inactive", call_args[0][0])

    @mock.patch("waldur_mastermind.marketplace_site_agent.tasks.logger")
    def test_no_logging_when_service_remains_active(self, mock_logger):
        """Test that no log message is generated when service remains active."""
        self.processor.last_run = timezone.now() - datetime.timedelta(minutes=5)
        self.processor.save()

        tasks.mark_agent_services_as_inactive()

        mock_logger.info.assert_not_called()

    def test_task_skips_services_with_no_processors(self):
        # Arrange - create service with no processors
        service_no_processors = factories.AgentServiceFactory(
            identity=self.agent_identity,
            state=enums.AgentServiceState.ACTIVE,
        )
        # Delete any auto-created processors
        service_no_processors.agentprocessor_set.all().delete()

        tasks.mark_agent_services_as_inactive()

        service_no_processors.refresh_from_db()
        self.assertEqual(service_no_processors.state, enums.AgentServiceState.ACTIVE)

    def test_task_skips_services_whose_processors_never_ran(self):
        """A processor that has never run must not be treated as stale."""
        self.processor.last_run = None
        self.processor.save()

        tasks.mark_agent_services_as_inactive()

        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.ACTIVE)

    def test_stale_processor_wins_over_processor_that_never_ran(self):
        """A service is still marked IDLE when only some of its processors have run."""
        self.processor.last_run = None
        self.processor.save()

        stale_processor = factories.AgentProcessorFactory(service=self.agent_service)
        stale_processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        stale_processor.save()

        tasks.mark_agent_services_as_inactive()

        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.state, enums.AgentServiceState.IDLE)

    def test_query_count_does_not_grow_with_number_of_services(self):
        """Guard against reintroducing a per-service processor lookup."""
        self.processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
        self.processor.save()

        with self.assertNumQueries(2):
            tasks.mark_agent_services_as_inactive()

        for _ in range(5):
            extra_service = factories.AgentServiceFactory(
                identity=self.agent_identity,
                state=enums.AgentServiceState.ACTIVE,
            )
            extra_processor = factories.AgentProcessorFactory(service=extra_service)
            extra_processor.last_run = timezone.now() - datetime.timedelta(minutes=15)
            extra_processor.save()

        with self.assertNumQueries(2):
            tasks.mark_agent_services_as_inactive()
