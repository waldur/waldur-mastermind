import json
import logging
import os
import tempfile
from enum import Enum

from constance import config
from docker.errors import ContainerError, DockerException
from rest_framework import serializers as rf_serializers

import docker
from waldur_kubernetes import backend as kubernetes_backend

from . import serializers
from .exceptions import JobFailedException

logger = logging.getLogger(__name__)

# The kubernetes client SDK is imported lazily inside the functions that use it
# (construct_k8s_job_spec / ContainerExecutorMixin.send_request), so it does not
# load at Django startup for deployments that never run scripts on Kubernetes.
# See CLAUDE.md, "Lazy imports for heavy optional backends".


class DeploymentOptions(Enum):
    DOCKER = "docker"
    KUBERNETES = "k8s"


WALDUR_K8S_LABELS = {
    "app.kubernetes.io/managed-by": "waldur",
    "app.kubernetes.io/component": "marketplace-script",
}


def check_docker_socket_access(docker_config):
    base_url = docker_config.get("base_url")

    # Check if base_url is using Unix socket
    if base_url and base_url.startswith("unix://"):
        # Extract socket path from the URL
        socket_path = base_url.replace("unix://", "")

        # Check if socket exists
        if not os.path.exists(socket_path):
            raise FileNotFoundError(f"Docker socket {socket_path} does not exist")

        # Check read and write permissions for current user
        if not os.access(socket_path, os.R_OK | os.W_OK):
            raise PermissionError(
                f"Current user doesn't have read/write permissions for Docker socket at {socket_path}"
            )


def execute_script_in_docker(image, command, src, **kwargs):
    remove_container = config.DOCKER_REMOVE_CONTAINER
    check_docker_socket_access(config.DOCKER_CLIENT)

    with tempfile.NamedTemporaryFile(
        prefix="docker",
        dir=config.DOCKER_SCRIPT_DIR,
        mode="w+",
    ) as docker_script:
        docker_script.write(src)
        docker_script.flush()
        logger.info(f"Wrote script to {docker_script.name}")
        client = docker.DockerClient(**config.DOCKER_CLIENT)
        docker_volume_name = config.DOCKER_VOLUME_NAME
        script_file_path = os.path.basename(docker_script.name)
        return str(
            client.containers.run(
                image=image,
                command=[command, script_file_path],
                remove=remove_container,
                stderr=True,
                working_dir="/work",
                volumes={
                    docker_volume_name: {
                        "bind": "/work",
                        "mode": "ro",
                    },
                },
                **config.DOCKER_RUN_OPTIONS,
                **kwargs,
            ),
            "utf-8",
        )


def construct_k8s_job_spec(image, command, volume_name, config_map_name, environment):
    import kubernetes as k8s

    script_volume = k8s.client.V1Volume(
        name=volume_name,
        config_map=k8s.client.V1ConfigMapVolumeSource(
            name=config_map_name,
            default_mode=0o0444,
        ),
    )
    env = [
        k8s.client.V1EnvVar(name=key, value=value) for key, value in environment.items()
    ]

    script_volume_mount = k8s.client.V1VolumeMount(
        name=volume_name, mount_path="/work/script", sub_path="script"
    )
    container = k8s.client.V1Container(
        name="runner",
        image=image,
        command=[command, "script"],
        volume_mounts=[script_volume_mount],
        working_dir="/work",
        env=env,
    )
    template = k8s.client.V1PodTemplateSpec(
        metadata=k8s.client.V1ObjectMeta(
            labels={"app": "waldur-marketplace-script-job"}
        ),
        spec=k8s.client.V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=[script_volume],
            resources=k8s.client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "256Mi"},
                limits={"cpu": "500m", "memory": "512Mi"},
            ),
        ),
    )
    spec = k8s.client.V1JobSpec(
        template=template,
        backoff_limit=0,  # Do not retry the job in case of failure
        active_deadline_seconds=config.K8S_JOB_TIMEOUT,
    )
    return spec


def execute_script_in_k8s(image, command, src, dry_run=False, **kwargs):
    """
    This function expects that Kubernetes config file located in path
    from constance config 'K8S_CONFIG_PATH' value
    """
    env = kwargs["environment"]
    job_name = f"job-{env['ORDER_UUID']}"
    config_map_name = f"script-{env['ORDER_UUID']}"
    volume_name = f"volume-{env['ORDER_UUID']}"

    k8s_backend = kubernetes_backend.KubernetesBackend(
        kubeconfig_file_path=config.K8S_CONFIG_PATH
    )

    config_map_data = {"script": src}
    k8s_backend.create_k8s_config_map(
        config_map_name, config.K8S_NAMESPACE, config_map_data, labels=WALDUR_K8S_LABELS
    )

    job_spec = construct_k8s_job_spec(image, command, volume_name, config_map_name, env)
    k8s_backend.create_k8s_job(
        job_name, config.K8S_NAMESPACE, job_spec, labels=WALDUR_K8S_LABELS
    )

    try:
        job_succeeded = k8s_backend.wait_for_k8s_job_completion(
            job_name, config.K8S_NAMESPACE, timeout=config.K8S_JOB_TIMEOUT
        )
        pod_log = k8s_backend.get_k8s_job_result(job_name, config.K8S_NAMESPACE)
    finally:
        try:
            k8s_backend.delete_job_from_k8s(job_name, config.K8S_NAMESPACE)
        except Exception:
            logger.exception(
                "Failed to delete Kubernetes Job %s during cleanup", job_name
            )
        try:
            k8s_backend.delete_config_map_from_k8s(
                config_map_name, config.K8S_NAMESPACE
            )
        except Exception:
            logger.exception(
                "Failed to delete Kubernetes ConfigMap %s during cleanup",
                config_map_name,
            )

    if not job_succeeded and not dry_run:
        raise JobFailedException(pod_log)
    return pod_log


def execute_script(image, command, src, dry_run=False, **kwargs):
    if config.SCRIPT_RUN_MODE == DeploymentOptions.DOCKER.value:
        return execute_script_in_docker(image, command, src, **kwargs)
    if config.SCRIPT_RUN_MODE == DeploymentOptions.KUBERNETES.value:
        return execute_script_in_k8s(image, command, src, dry_run=dry_run, **kwargs)


class ContainerExecutorMixin:
    """Mixin to execute scripts in containers for marketplace script processing."""

    hook_type = NotImplemented

    def send_request(self, user, resource=None, dry_run=False):
        from kubernetes.client.rest import ApiException

        options = self.order.offering.secret_options

        serializer = serializers.OrderSerializer(instance=self.order)
        input_parameters = dict(
            serializer.data
        )  # drop the self-reference to serializer by converting to dict
        environment = {
            key.upper(): input_parameters[key] for key in input_parameters.keys()
        }
        # update environment with offering-specific parameters
        for opt in options.get("environ", []):
            if isinstance(opt, dict):
                environment.update({opt["name"]: opt["value"]})

        language = options["language"]
        image = config.DOCKER_IMAGES.get(language)["image"]
        command = config.DOCKER_IMAGES.get(language)["command"]

        logger.debug(
            "About to execute marketplace script via Docker. "
            "Hook type is %s. Order ID is %s.",
            self.hook_type,
            self.order.id,
        )

        try:
            environment = {
                key: json.dumps(value) if isinstance(value, dict | list) else str(value)
                for key, value in environment.items()
            }
            output = execute_script(
                image=image,
                command=command,
                src=options[self.hook_type],
                dry_run=dry_run,
                environment=environment,
            )
            if dry_run:
                return output
            self.order.output = output
            self.order.save(update_fields=["output"])

        except ContainerError as exc:
            self.order.output = str(exc)
            self.order.save(update_fields=["output"])
            logger.exception(
                "Unable to execute marketplace script via Docker. "
                "Hook type is %s. Order ID is %s.",
                self.hook_type,
                self.order.id,
            )
            raise rf_serializers.ValidationError(str(exc))

        except DockerException as exc:
            logger.exception(
                "Unable to execute marketplace script via Docker. "
                "Hook type is %s. Order ID is %s.",
                self.hook_type,
                self.order.id,
            )
            raise rf_serializers.ValidationError(str(exc))

        except ApiException as exc:
            logger.exception(
                "Unable to execute marketplace script via Kubernetes. "
                "Hook type is %s. Order ID is %s.",
                self.hook_type,
                self.order.id,
            )
            raise rf_serializers.ValidationError(str(exc))

        logger.debug(
            "Successfully executed marketplace script via Docker."
            "Hook type is %s. Order ID is %s.",
            self.hook_type,
            self.order.id,
        )
        return self.order.output

    def validate_order(self, request):
        options = self.order.offering.secret_options

        if self.hook_type not in options:
            raise rf_serializers.ValidationError("Script is not defined.")

        command = config.DOCKER_IMAGES.get(options["language"])
        if not command:
            raise rf_serializers.ValidationError("Docker image is not allowed.")

        src = self.order.offering.secret_options[self.hook_type]
        if len(src.encode("utf-8")) > 1024 * 1024:
            raise rf_serializers.ValidationError(
                "The length of script is more than 1 MB."
            )
