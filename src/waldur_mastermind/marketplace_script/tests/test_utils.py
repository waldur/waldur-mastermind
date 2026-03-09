from unittest.mock import MagicMock, patch

from django.test import TestCase

from waldur_kubernetes.exceptions import KubernetesException
from waldur_mastermind.marketplace_script.utils import execute_script_in_k8s


class ExecuteScriptInK8sCleanupTest(TestCase):
    """Tests that cleanup (delete job/configmap) always runs even when wait raises."""

    def _make_k8s_backend(self):
        backend = MagicMock()
        backend.create_k8s_config_map.return_value = None
        backend.create_k8s_job.return_value = None
        return backend

    @patch("waldur_mastermind.marketplace_script.utils.kubernetes_backend")
    @patch("waldur_mastermind.marketplace_script.utils.config")
    def test_cleanup_runs_when_wait_raises(self, mock_config, mock_kb_module):
        mock_config.K8S_CONFIG_PATH = "/fake/kubeconfig"
        mock_config.K8S_NAMESPACE = "test-namespace"
        mock_config.K8S_JOB_TIMEOUT = 600

        mock_backend = self._make_k8s_backend()
        mock_backend.wait_for_k8s_job_completion.side_effect = KubernetesException(
            "Job timed out"
        )
        mock_kb_module.KubernetesBackend.return_value = mock_backend

        environment = {"ORDER_UUID": "test-order-uuid-abc12345"}

        with self.assertRaises(KubernetesException):
            execute_script_in_k8s(
                image="python:3.11",
                command="python",
                src="print('hello')",
                dry_run=False,
                environment=environment,
            )

        mock_backend.delete_job_from_k8s.assert_called_once_with(
            "job-test-order-uuid-abc12345", "test-namespace"
        )
        mock_backend.delete_config_map_from_k8s.assert_called_once_with(
            "script-test-order-uuid-abc12345", "test-namespace"
        )
