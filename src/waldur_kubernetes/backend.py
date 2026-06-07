import logging
import time
from typing import cast

import kubernetes as k8s
import yaml
from django.core import exceptions as django_exceptions

from waldur_kubernetes.exceptions import KubernetesException

logger = logging.getLogger(__name__)


class KubernetesBackend:
    def __init__(
        self, kubeconfig_str: str | None = None, kubeconfig_file_path: str | None = None
    ):
        if not kubeconfig_str and not kubeconfig_file_path:
            raise KubernetesException(
                "Either kubeconfig_str or kubeconfig_file_path should be provided"
            )
        if kubeconfig_str:
            kubeconfig_dict = yaml.safe_load(kubeconfig_str)
            k8s.config.load_kube_config_from_dict(config_dict=kubeconfig_dict)
        else:
            try:
                k8s.config.load_kube_config(config_file=kubeconfig_file_path)
            except k8s.config.config_exception.ConfigException:
                logger.info(
                    "Failed to load kubeconfig from %s, trying in-cluster config",
                    kubeconfig_file_path,
                )
                try:
                    k8s.config.load_incluster_config()
                except k8s.config.config_exception.ConfigException as e:
                    raise KubernetesException(
                        f"Failed to load Kubernetes config from {kubeconfig_file_path} "
                        f"and in-cluster config: {e}"
                    )
        self.core_api = k8s.client.CoreV1Api()
        self.batch_v1_api = k8s.client.BatchV1Api()

    def get_k8s_secret(
        self,
        name: str,
        namespace: str,
    ):
        try:
            existing_secret = self.core_api.read_namespaced_secret(
                name=name,
                namespace=namespace,
            )
            return existing_secret
        except k8s.client.ApiException as e:
            if e.status == 404:
                logger.info(
                    "K8s secret %s not found in the namespace %s", name, namespace
                )
                return None
            raise KubernetesException(e)

    def create_k8s_secret(
        self,
        name: str,
        namespace: str,
        labels: dict | None = None,
        data: dict | None = None,
        string_data: dict | None = None,
    ):
        secret = k8s.client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=k8s.client.V1ObjectMeta(name=name, labels=labels),
            data=data,
            string_data=string_data,
        )
        self.core_api.create_namespaced_secret(namespace=namespace, body=secret)

    def update_k8s_secret(
        self,
        name: str,
        namespace: str,
        data: dict | None = None,
        labels: dict | None = None,
    ):
        secret_object = cast(
            k8s.client.V1Secret | None, self.get_k8s_secret(name, namespace)
        )
        if secret_object is None:
            raise django_exceptions.ObjectDoesNotExist(
                f"Secret {name} not found in namespace {namespace}"
            )
        if labels is not None:
            # Merge labels
            existing_labels = secret_object.metadata.labels
            labels.update(existing_labels)
            secret_object.metadata.labels = labels
        if data:
            # Overwrite secret content
            secret_object.data = data
        self.core_api.patch_namespaced_secret(
            name=name, namespace=namespace, body=secret_object
        )

    def create_or_update_k8s_secret(self, name, namespace, data, labels):
        existing_secret: k8s.client.V1Secret | None = cast(
            k8s.client.V1Secret | None, self.get_k8s_secret(name, namespace)
        )
        if existing_secret:
            self.update_k8s_secret(name, namespace, None, labels)
        else:
            self.create_k8s_secret(name, namespace, data, labels)

    def create_k8s_config_map(self, name, namespace, data, labels=None):
        config_map = k8s.client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=k8s.client.V1ObjectMeta(name=name, labels=labels),
            data=data,
        )
        try:
            self.core_api.create_namespaced_config_map(
                body=config_map,
                namespace=namespace,
            )
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to create a ConfigMap %s in the Kubernetes namespace %s. Reason: %s",
                name,
                namespace,
                e,
            )
            raise KubernetesException(e)
        else:
            logger.info(
                "ConfigMap %s has been created in Kubernetes namespace %s",
                config_map.metadata.name,
                namespace,
            )

    def delete_config_map_from_k8s(self, name: str, namespace: str):
        try:
            self.core_api.delete_namespaced_config_map(name, namespace)
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to delete a ConfigMap %s in the Kubernetes namespace %s. Reason: %s",
                name,
                namespace,
                e,
            )
            raise KubernetesException(e)
        else:
            logger.info(
                "ConfigMap %s has been deleted from namespace %s",
                name,
                namespace,
            )

    def list_k8s_jobs(self, namespace: str, label_selector: str | None = None) -> list:
        try:
            response = self.batch_v1_api.list_namespaced_job(
                namespace,
                label_selector=label_selector,
            )
            return response.items
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to list Jobs in the Kubernetes namespace %s. Reason: %s",
                namespace,
                e,
            )
            raise KubernetesException(e)

    def list_k8s_config_maps(
        self, namespace: str, label_selector: str | None = None
    ) -> list:
        try:
            response = self.core_api.list_namespaced_config_map(
                namespace,
                label_selector=label_selector,
            )
            return response.items
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to list ConfigMaps in the Kubernetes namespace %s. Reason: %s",
                namespace,
                e,
            )
            raise KubernetesException(e)

    def create_k8s_job(
        self,
        name: str,
        namespace: str,
        spec: k8s.client.V1JobSpec,
        labels: dict | None = None,
    ):
        job = k8s.client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s.client.V1ObjectMeta(name=name, labels=labels),
            spec=spec,
        )
        try:
            self.batch_v1_api.create_namespaced_job(body=job, namespace=namespace)
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to create a Job %s in the Kubernetes namespace %s. Reason: %s",
                name,
                namespace,
                e,
            )
            raise KubernetesException(e)
        else:
            logger.info(
                "Job %s has been created in Kubernetes namespace %s",
                job.metadata.name,
                namespace,
            )

    def delete_job_from_k8s(self, name: str, namespace: str):
        try:
            self.batch_v1_api.delete_namespaced_job(
                name=name, namespace=namespace, propagation_policy="Background"
            )
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to delete Job %s in the Kubernetes namespace %s. Reason: %s",
                name,
                namespace,
                e,
            )
            raise KubernetesException(e)
        else:
            logger.info("Job %s has been deleted from namespace %s", name, namespace)

    def get_k8s_job_result(self, name: str, namespace: str):
        try:
            pods = self.core_api.list_namespaced_pod(
                namespace, label_selector=f"job-name={name}"
            )
            pod_name = pods.items[
                0
            ].metadata.name  # The number of containers is 1, because backoff limit is 0
            log = self.core_api.read_namespaced_pod_log(pod_name, namespace)
        except k8s.client.ApiException as e:
            logger.error(
                "Unable to get pods logs for job %s in namespace %s. Reason: %s",
                name,
                namespace,
                e,
            )
            raise KubernetesException(e)
        else:
            return log

    def wait_for_k8s_job_completion(
        self, name: str, namespace: str, timeout: int = 600
    ):
        job_succeeded = None
        waited = 0
        while True:
            api_response = cast(
                k8s.client.V1Job,
                self.batch_v1_api.read_namespaced_job_status(
                    name=name,
                    namespace=namespace,
                ),
            )
            if api_response.status.succeeded is not None:
                job_succeeded = True
                break
            if api_response.status.failed is not None:
                job_succeeded = False
                break
            time.sleep(10)
            waited += 10
            if waited >= timeout:
                raise KubernetesException(
                    f"Job {name} in namespace {namespace} has not completed in {timeout} seconds."
                )
        logger.info(
            "Job %s in namespace %s completed with status %s",
            name,
            namespace,
            "succeeded" if job_succeeded else "failed",
        )
        return job_succeeded
