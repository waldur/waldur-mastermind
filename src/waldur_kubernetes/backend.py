import logging
from typing import cast

import kubernetes as k8s
import yaml
from django.core import exceptions as django_exceptions

logger = logging.getLogger(__name__)


class KubernetesBackend:
    def __init__(self, kubeconfig_str: str):
        kubeconfig_dict = yaml.safe_load(kubeconfig_str)
        k8s.config.load_kube_config_from_dict(config_dict=kubeconfig_dict)
        self.core_api = k8s.client.CoreV1Api()

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
            raise

    def create_k8s_secret(
        self,
        name: str,
        namespace: str,
        labels: dict = None,
        data: dict = None,
        string_data: dict = None,
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
        existing_secret: k8s.client.V1Secret = self.get_k8s_secret(name, namespace)
        if existing_secret:
            self.update_k8s_secret(name, namespace, None, labels)
        else:
            self.create_k8s_secret(name, namespace, data, labels)
