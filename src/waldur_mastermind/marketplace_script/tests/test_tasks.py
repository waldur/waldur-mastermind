from datetime import timedelta
from unittest import mock
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework import test

from waldur_kubernetes.exceptions import KubernetesException
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace_script.tasks import (
    cleanup_orphaned_k8s_resources,
    pull_resource,
    resource_options_have_been_changed,
)
from waldur_mastermind.marketplace_script.utils import DeploymentOptions

from . import fixtures


@mock.patch("waldur_mastermind.marketplace_script.utils.execute_script")
class PullResourceTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScriptFixture()
        self.offering = self.fixture.offering
        self.offering.options.update({"pull": "raise Exception()"})
        self.resource = self.fixture.resource

    def test_set_state_erred(self, execute_script):
        execute_script.side_effect = Exception("Container exception")
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.ERRED)
        self.assertEqual(self.resource.error_message, "Container exception")

    def test_set_state_ok(self, execute_script):
        self.resource.state = ResourceStates.ERRED
        self.resource.save()
        execute_script.return_value = ""
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.error_message, "")


class ResourceOptionsHandlerTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScriptFixture()
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource

    @mock.patch(
        "waldur_mastermind.marketplace_script.handlers.tasks.resource_options_have_been_changed"
    )
    def test_task_has_been_running_if_resource_options_has_been_changed(
        self, mock_task
    ):
        self.resource.options = {"key": "value"}
        self.resource.save()
        mock_task.delay.assert_called_once_with(self.resource.id, None)

    @mock.patch("waldur_mastermind.marketplace_script.utils.execute_script")
    def test_task(self, execute_script):
        self.resource.options = {"key": "value"}
        self.resource.save()
        resource_options_have_been_changed(self.resource.id, None)
        execute_script.assert_called_once()


class CleanupOrphanedK8sResourcesTest(TestCase):
    def _make_job(self, name, creation_timestamp):
        job = MagicMock()
        job.metadata.name = name
        job.metadata.creation_timestamp = creation_timestamp
        return job

    def _make_config_map(self, name, creation_timestamp):
        cm = MagicMock()
        cm.metadata.name = name
        cm.metadata.creation_timestamp = creation_timestamp
        return cm

    @patch("waldur_mastermind.marketplace_script.tasks.config")
    def test_skips_when_not_kubernetes_mode(self, mock_config):
        mock_config.SCRIPT_RUN_MODE = DeploymentOptions.DOCKER.value
        with patch(
            "waldur_mastermind.marketplace_script.tasks.kubernetes_backend.KubernetesBackend"
        ) as mock_backend_cls:
            cleanup_orphaned_k8s_resources()
            mock_backend_cls.assert_not_called()

    @patch("waldur_mastermind.marketplace_script.tasks.config")
    @patch(
        "waldur_mastermind.marketplace_script.tasks.kubernetes_backend.KubernetesBackend"
    )
    def test_deletes_old_jobs_not_recent(self, mock_backend_cls, mock_config):
        mock_config.SCRIPT_RUN_MODE = DeploymentOptions.KUBERNETES.value
        mock_config.K8S_CONFIG_PATH = "/fake/kubeconfig"
        mock_config.K8S_NAMESPACE = "test-namespace"

        now = timezone.now()
        old_job = self._make_job("old-job", now - timedelta(hours=2))
        recent_job = self._make_job("recent-job", now - timedelta(minutes=30))

        mock_backend = MagicMock()
        mock_backend_cls.return_value = mock_backend
        mock_backend.list_k8s_jobs.return_value = [old_job, recent_job]
        mock_backend.list_k8s_config_maps.return_value = []

        cleanup_orphaned_k8s_resources()

        mock_backend.delete_job_from_k8s.assert_called_once_with(
            "old-job", "test-namespace"
        )

    @patch("waldur_mastermind.marketplace_script.tasks.config")
    @patch(
        "waldur_mastermind.marketplace_script.tasks.kubernetes_backend.KubernetesBackend"
    )
    def test_deletes_old_config_maps(self, mock_backend_cls, mock_config):
        mock_config.SCRIPT_RUN_MODE = DeploymentOptions.KUBERNETES.value
        mock_config.K8S_CONFIG_PATH = "/fake/kubeconfig"
        mock_config.K8S_NAMESPACE = "test-namespace"

        now = timezone.now()
        old_cm = self._make_config_map("old-cm", now - timedelta(hours=2))

        mock_backend = MagicMock()
        mock_backend_cls.return_value = mock_backend
        mock_backend.list_k8s_jobs.return_value = []
        mock_backend.list_k8s_config_maps.return_value = [old_cm]

        cleanup_orphaned_k8s_resources()

        mock_backend.delete_config_map_from_k8s.assert_called_once_with(
            "old-cm", "test-namespace"
        )

    @patch("waldur_mastermind.marketplace_script.tasks.config")
    @patch(
        "waldur_mastermind.marketplace_script.tasks.kubernetes_backend.KubernetesBackend"
    )
    def test_continues_after_per_resource_deletion_failure(
        self, mock_backend_cls, mock_config
    ):
        mock_config.SCRIPT_RUN_MODE = DeploymentOptions.KUBERNETES.value
        mock_config.K8S_CONFIG_PATH = "/fake/kubeconfig"
        mock_config.K8S_NAMESPACE = "test-namespace"

        now = timezone.now()
        old_job1 = self._make_job("old-job-1", now - timedelta(hours=3))
        old_job2 = self._make_job("old-job-2", now - timedelta(hours=2))

        mock_backend = MagicMock()
        mock_backend_cls.return_value = mock_backend
        mock_backend.list_k8s_jobs.return_value = [old_job1, old_job2]
        mock_backend.list_k8s_config_maps.return_value = []
        mock_backend.delete_job_from_k8s.side_effect = KubernetesException(
            "delete failed"
        )

        # Should not raise; both deletions are attempted
        cleanup_orphaned_k8s_resources()

        self.assertEqual(mock_backend.delete_job_from_k8s.call_count, 2)

    @patch("waldur_mastermind.marketplace_script.tasks.config")
    @patch(
        "waldur_mastermind.marketplace_script.tasks.kubernetes_backend.KubernetesBackend"
    )
    def test_skips_when_backend_init_fails(self, mock_backend_cls, mock_config):
        mock_config.SCRIPT_RUN_MODE = DeploymentOptions.KUBERNETES.value
        mock_config.K8S_CONFIG_PATH = "/fake/kubeconfig"
        mock_config.K8S_NAMESPACE = "test-namespace"
        mock_backend_cls.side_effect = KubernetesException("kubeconfig unavailable")

        # Should not raise; task exits early without touching the backend
        cleanup_orphaned_k8s_resources()

        mock_backend_cls.assert_called_once()

    @patch("waldur_mastermind.marketplace_script.tasks.config")
    @patch(
        "waldur_mastermind.marketplace_script.tasks.kubernetes_backend.KubernetesBackend"
    )
    def test_continues_to_config_maps_if_job_list_fails(
        self, mock_backend_cls, mock_config
    ):
        mock_config.SCRIPT_RUN_MODE = DeploymentOptions.KUBERNETES.value
        mock_config.K8S_CONFIG_PATH = "/fake/kubeconfig"
        mock_config.K8S_NAMESPACE = "test-namespace"

        now = timezone.now()
        old_cm = self._make_config_map("old-cm", now - timedelta(hours=2))

        mock_backend = MagicMock()
        mock_backend_cls.return_value = mock_backend
        mock_backend.list_k8s_jobs.side_effect = KubernetesException("list jobs failed")
        mock_backend.list_k8s_config_maps.return_value = [old_cm]

        cleanup_orphaned_k8s_resources()

        mock_backend.delete_config_map_from_k8s.assert_called_once_with(
            "old-cm", "test-namespace"
        )


class UniqueOrderUuidTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ScriptFixture()
        self.resource = self.fixture.resource

    @patch("waldur_mastermind.marketplace_script.utils.execute_script")
    def test_pull_resource_generates_unique_order_uuid(self, mock_execute_script):
        mock_execute_script.return_value = ""
        captured_uuids = []

        def capture_kwargs(*args, **kwargs):
            captured_uuids.append(kwargs.get("environment", {}).get("ORDER_UUID"))
            return ""

        mock_execute_script.side_effect = capture_kwargs

        pull_resource(self.resource.id)
        pull_resource(self.resource.id)

        self.assertEqual(len(captured_uuids), 2)
        self.assertIsNotNone(captured_uuids[0])
        self.assertIsNotNone(captured_uuids[1])
        self.assertNotEqual(
            captured_uuids[0],
            captured_uuids[1],
            "ORDER_UUID must be unique across separate pull_resource invocations",
        )

    @patch("waldur_mastermind.marketplace_script.utils.execute_script")
    def test_resource_options_handler_generates_unique_order_uuid(
        self, mock_execute_script
    ):
        mock_execute_script.return_value = ""
        captured_uuids = []

        def capture_kwargs(*args, **kwargs):
            captured_uuids.append(kwargs.get("environment", {}).get("ORDER_UUID"))
            return ""

        mock_execute_script.side_effect = capture_kwargs

        resource_options_have_been_changed(self.resource.id, None)
        resource_options_have_been_changed(self.resource.id, None)

        self.assertEqual(len(captured_uuids), 2)
        self.assertIsNotNone(captured_uuids[0])
        self.assertIsNotNone(captured_uuids[1])
        self.assertNotEqual(
            captured_uuids[0],
            captured_uuids[1],
            "ORDER_UUID must be unique across separate resource_options_have_been_changed invocations",
        )
