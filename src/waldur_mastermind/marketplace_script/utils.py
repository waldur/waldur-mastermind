import json
import logging
import tempfile
from enum import Enum
from time import sleep

import docker
from django.conf import settings
from docker.errors import DockerException
from kubernetes import kubernetes as k8s
from kubernetes.client.rest import ApiException
from rest_framework import serializers as rf_serializers

from . import serializers
from .exceptions import JobFailedException

logger = logging.getLogger(__name__)

NAMESPACE = settings.WALDUR_MARKETPLACE_SCRIPT['K8S_NAMESPACE']


class DeploymentOptions(Enum):
    DOCKER = 'docker'
    KUBERNETES = 'k8s'


def execute_script_in_docker(image, command, src, **kwargs):
    with tempfile.NamedTemporaryFile(
        prefix='docker',
        dir=settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_SCRIPT_DIR'],
        mode="w+",
    ) as docker_script:
        docker_script.write(src)
        docker_script.flush()
        client = docker.DockerClient(
            **settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_CLIENT']
        )
        return str(
            client.containers.run(
                image=image,
                command=[command, 'script'],
                remove=True,
                working_dir="/work",
                volumes={
                    docker_script.name: {
                        "bind": "/work/script",
                        "mode": "ro",
                    },
                },
                **settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_RUN_OPTIONS'],
                **kwargs,
            ),
            'utf-8',
        )


def construct_k8s_config_map(name, src):
    return k8s.client.V1ConfigMap(
        api_version='v1',
        kind='ConfigMap',
        metadata=k8s.client.V1ObjectMeta(name=name),
        data={'script': src},
    )


def construct_k8s_job(name, image, command, volume_name, config_map_name, environment):
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
        name=volume_name, mount_path='/work/script', sub_path='script'
    )
    container = k8s.client.V1Container(
        name='runner',
        image=image,
        command=[command, 'script'],
        volume_mounts=[script_volume_mount],
        working_dir='/work',
        env=env,
    )
    template = k8s.client.V1PodTemplateSpec(
        metadata=k8s.client.V1ObjectMeta(
            labels={'app': 'waldur-marketplace-script-job'}
        ),
        spec=k8s.client.V1PodSpec(
            restart_policy='Never', containers=[container], volumes=[script_volume]
        ),
    )
    spec = k8s.client.V1JobSpec(
        template=template,
        backoff_limit=0,  # Do not retry the job in case of failure
        active_deadline_seconds=settings.WALDUR_MARKETPLACE_SCRIPT['K8S_JOB_TIMEOUT'],
    )
    return k8s.client.V1Job(
        api_version='batch/v1',
        kind='Job',
        metadata=k8s.client.V1ObjectMeta(name=name),
        spec=spec,
    )


def create_job_in_k8s(batch_api: k8s.client.BatchV1Api, job_object):
    batch_api.create_namespaced_job(body=job_object, namespace=NAMESPACE)
    logger.info(
        'Job %s has been created in namespace %s',
        job_object.metadata.name,
        NAMESPACE,
    )


def create_config_map_in_k8s(api: k8s.client.CoreV1Api, config_map_object):
    api.create_namespaced_config_map(
        body=config_map_object,
        namespace=NAMESPACE,
    )
    logger.info(
        'ConfigMap %s has been created in namespace %s',
        config_map_object.metadata.name,
        NAMESPACE,
    )


def delete_job_from_k8s(batch_api: k8s.client.BatchV1Api, job_name):
    batch_api.delete_namespaced_job(
        name=job_name, namespace=NAMESPACE, propagation_policy='Background'
    )
    logger.info('Job %s has been deleted from namespace %s', job_name, NAMESPACE)


def delete_config_map_from_k8s(api: k8s.client.CoreV1Api, config_map_name):
    api.delete_namespaced_config_map(config_map_name, NAMESPACE)
    logger.info(
        'ConfigMap %s has been deleted from namespace %s',
        config_map_name,
        NAMESPACE,
    )


def wait_for_k8s_job_completion(batch_api: k8s.client.BatchV1Api, job_name):
    job_succeeded = None
    while True:
        api_response = batch_api.read_namespaced_job_status(
            name=job_name,
            namespace=NAMESPACE,
        )
        if api_response.status.succeeded is not None:
            job_succeeded = True
            break
        if api_response.status.failed is not None:
            job_succeeded = False
            break
        sleep(10)
    logger.info(
        "Job %s in namespace %s completed with status %s",
        job_name,
        NAMESPACE,
        'succeeded' if job_succeeded else 'failed',
    )
    return job_succeeded


def get_k8s_job_result(api: k8s.client.CoreV1Api, job_name):
    pods = api.list_namespaced_pod(NAMESPACE, label_selector='job-name=%s' % job_name)
    pod_name = pods.items[
        0
    ].metadata.name  # The number of containers is 1, because backoff limit is 0
    log = api.read_namespaced_pod_log(pod_name, NAMESPACE)
    return log


def execute_script_in_k8s(image, command, src, **kwargs):
    """
    This function expects that Kubernetes config file located in path
    from settings.WALDUR_MARKETPLACE_SCRIPT['K8S_CONFIG_PATH'] value
    """
    env = kwargs['environment']
    job_name = 'job-%s' % env['ORDER_ITEM_UUID']
    config_map_name = 'script-%s' % env['ORDER_ITEM_UUID']
    volume_name = 'volume-%s' % env['ORDER_ITEM_UUID']

    k8s.config.load_kube_config(
        config_file=settings.WALDUR_MARKETPLACE_SCRIPT['K8S_CONFIG_PATH']
    )
    batch_v1_api = k8s.client.BatchV1Api()
    api_v1 = k8s.client.CoreV1Api()

    config_map_object = construct_k8s_config_map(config_map_name, src)

    job_object = construct_k8s_job(
        job_name, image, command, volume_name, config_map_name, env
    )

    create_config_map_in_k8s(api_v1, config_map_object)
    create_job_in_k8s(batch_v1_api, job_object)

    job_succeeded = wait_for_k8s_job_completion(batch_v1_api, job_name)
    pod_log = get_k8s_job_result(api_v1, job_name)

    delete_job_from_k8s(batch_v1_api, job_name)
    delete_config_map_from_k8s(api_v1, config_map_name)

    if not job_succeeded:
        raise JobFailedException(pod_log)
    return pod_log


def execute_script(image, command, src, **kwargs):
    if (
        settings.WALDUR_MARKETPLACE_SCRIPT['SCRIPT_RUN_MODE']
        == DeploymentOptions.DOCKER.value
    ):
        return execute_script_in_docker(image, command, src, **kwargs)
    if (
        settings.WALDUR_MARKETPLACE_SCRIPT['SCRIPT_RUN_MODE']
        == DeploymentOptions.KUBERNETES.value
    ):
        return execute_script_in_k8s(image, command, src, **kwargs)


class ContainerExecutorMixin:
    hook_type = NotImplemented

    def send_request(self, user, resource=None):
        options = self.order_item.offering.secret_options

        serializer = serializers.OrderItemSerializer(instance=self.order_item)
        input_parameters = dict(
            serializer.data
        )  # drop the self-reference to serializer by converting to dict
        input_parameters.update({'order_item_uuid': self.order_item.uuid.hex})
        environment = {
            key.upper(): input_parameters[key] for key in input_parameters.keys()
        }
        # update environment with offering-specific parameters
        for opt in options.get('environ', []):
            if isinstance(opt, dict):
                environment.update({opt['name']: opt['value']})

        language = options['language']
        image = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(language)
        logger.debug(
            'About to execute marketplace script via Docker. '
            'Hook type is %s. Order item ID is %s.',
            self.hook_type,
            self.order_item.id,
        )

        try:
            environment = {
                key: json.dumps(value)
                if isinstance(value, (dict, list))
                else str(value)
                for key, value in environment.items()
            }

            self.order_item.output = execute_script(
                image=image,
                command=language,
                src=options[self.hook_type],
                environment=environment,
            )
            self.order_item.save(update_fields=['output'])
        except DockerException as exc:
            logger.exception(
                'Unable to execute marketplace script via Docker. '
                'Hook type is %s. Order item ID is %s.',
                self.hook_type,
                self.order_item.id,
            )
            raise rf_serializers.ValidationError(str(exc))
        except ApiException as exc:
            logger.exception(
                'Unable to execute marketplace script via Kubernetes. '
                'Hook type is %s. Order item ID is %s.',
                self.hook_type,
                self.order_item.id,
            )
            raise rf_serializers.ValidationError(str(exc))
        logger.debug(
            'Successfully executed marketplace script via Docker.'
            'Hook type is %s. Order item ID is %s.',
            self.hook_type,
            self.order_item.id,
        )
        return self.order_item.output

    def validate_order_item(self, request):
        options = self.order_item.offering.secret_options

        if self.hook_type not in options:
            raise rf_serializers.ValidationError('Script is not defined.')

        command = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(
            options['language']
        )
        if not command:
            raise rf_serializers.ValidationError('Docker image is not allowed.')

        src = self.order_item.offering.secret_options[self.hook_type]
        if len(src.encode('utf-8')) > 1024 * 1024:
            raise rf_serializers.ValidationError(
                'The length of script is more than 1 MB.'
            )
