import base64
import json
import logging
import uuid

from celery import shared_task
from constance import config
from django.utils import timezone

from waldur_core.core.utils import get_fake_context, get_system_robot
from waldur_core.structure import models as structure_models
from waldur_kubernetes import backend as kubernetes_backend
from waldur_kubernetes.exceptions import KubernetesException
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import serializers as marketplace_serializer
from waldur_mastermind.marketplace.enums import SCRIPT_OFFERING, ResourceStates
from waldur_mastermind.marketplace_script import models as marketplace_script_models
from waldur_mastermind.marketplace_script import serializers, utils
from waldur_mastermind.marketplace_script.utils import (
    WALDUR_K8S_LABELS,
    DeploymentOptions,
)

logger = logging.getLogger(__name__)


@shared_task(name="waldur_marketplace_script.pull_resources")
def pull_resources():
    """Pull resources from marketplace script offerings by executing configured pull scripts."""
    for resource in models.Resource.objects.filter(
        offering__type=SCRIPT_OFFERING,
        offering__secret_options__has_key="pull",
        state__in=[ResourceStates.OK, ResourceStates.ERRED],
    ):
        pull_resource.delay(resource.id)


@shared_task
def pull_resource(resource_id):
    resource = models.Resource.objects.get(id=resource_id)

    # We use secret_options the same like in ContainerExecutorMixin.send_request
    options = resource.offering.secret_options
    if "pull" not in options:
        logger.debug("Missing pull script, skipping")
        return
    serializer = serializers.ResourceSerializer(instance=resource)
    environment = {
        key.upper(): json.dumps(value) if isinstance(value, dict | list) else str(value)
        for key, value in dict(serializer.data).items()
    }
    # ORDER_UUID is required for k8s job naming; use resource UUID for pull operations
    environment["ORDER_UUID"] = f"{resource.uuid}-{uuid.uuid4().hex[:8]}"
    for opt in options.get("environ", []):
        if isinstance(opt, dict):
            environment.update({opt["name"]: opt["value"]})

    language = options["language"]
    image = config.DOCKER_IMAGES.get(language)["image"]
    command = config.DOCKER_IMAGES.get(language)["command"]

    try:
        output = utils.execute_script(
            image=image, command=command, src=options["pull"], environment=environment
        )
        if output:
            last_line = output.splitlines()[-1]
            decoded_metadata = base64.b64decode(last_line)
            updated_values = json.loads(decoded_metadata)
            context = get_fake_context(user=get_system_robot())
            if "usages" in updated_values.keys():
                new_usages = updated_values["usages"]
                rpp = models.ResourcePlanPeriod.objects.get(
                    resource=resource, plan=resource.plan
                ).uuid
                usage_serializer = (
                    marketplace_serializer.ComponentUsageCreateSerializer(
                        data={"usages": new_usages, "plan_period": rpp}, context=context
                    )
                )
                if usage_serializer.is_valid():
                    usage_serializer.save()
                else:
                    logger.error(
                        f"Validation failed when processing reported usage for {resource},"
                        f" usage values {new_usages}, validation errors: {usage_serializer.errors}"
                    )
            if "report" in updated_values.keys():
                new_report = updated_values["report"]
                report_serializer = marketplace_serializer.ResourceReportSerializer(
                    data={"report": new_report}, context=context
                )
                if report_serializer.is_valid():
                    resource.report = report_serializer.validated_data["report"]
                    resource.save(update_fields=["report"])
                else:
                    logger.error(
                        f"Validation failed when processing report for {resource},"
                        f"{new_report}, validation errors: {report_serializer.errors}"
                    )
    except Exception as e:
        resource.set_state_erred()
        if e:
            resource.error_message = str(e).splitlines()[0]
            resource.error_traceback = str(e)
    else:
        if resource.state != ResourceStates.OK:
            resource.set_state_ok()
            resource.error_message = ""
            resource.error_traceback = ""
    finally:
        resource.save()


@shared_task
def dry_run_executor(dry_run_id):
    dry_run = marketplace_script_models.DryRun.objects.get(id=dry_run_id)
    dry_run.set_state_executing()
    dry_run.save()
    order = dry_run.order
    executor = utils.ContainerExecutorMixin()
    executor.order = order
    executor.hook_type = dry_run.order_type
    try:
        dry_run.output = executor.send_request(dry_run.order.created_by, dry_run=True)
    except Exception as exc:
        logger.error("An unexpected error occurred while executing dry run: %s", exc)
        raise
    finally:
        dry_run.save()
        structure_models.Project.objects.filter(id=dry_run.order.project.id).delete()


@shared_task(name="waldur_marketplace_script.remove_old_dry_runs")
def remove_old_dry_runs():
    """Remove old dry run records that are older than one day."""
    marketplace_script_models.DryRun.objects.filter(
        state=marketplace_script_models.DryRun.States.DONE,
        created__lt=timezone.now() - timezone.timedelta(days=1),
    ).delete()


@shared_task
def resource_options_have_been_changed(resource_id, options_old):
    resource = models.Resource.objects.get(id=resource_id)

    options = resource.offering.secret_options
    if "resource_options_handler" not in options:
        logger.debug("Missing resource options handler script, skipping")
        return
    serializer = serializers.ResourceSerializer(instance=resource)
    environment = {
        key.upper(): json.dumps(value) if isinstance(value, dict | list) else str(value)
        for key, value in dict(serializer.data).items()
    }
    # ORDER_UUID is required for k8s job naming; use resource UUID for options handler
    environment["ORDER_UUID"] = f"{resource.uuid}-{uuid.uuid4().hex[:8]}"
    for opt in options.get("environ", []):
        if isinstance(opt, dict):
            environment.update({opt["name"]: opt["value"]})
    environment["RESOURCE_OPTIONS_OLD"] = options_old
    environment["RESOURCE_OPTIONS"] = resource.options

    language = options["language"]
    image = config.DOCKER_IMAGES.get(language)["image"]
    command = config.DOCKER_IMAGES.get(language)["command"]

    try:
        utils.execute_script(
            image=image,
            command=command,
            src=options["resource_options_handler"],
            environment=environment,
        )
    except Exception as e:
        resource.set_state_erred()
        if e:
            resource.error_message = str(e).splitlines()[0]
            resource.error_traceback = str(e)
    else:
        if resource.state != ResourceStates.OK:
            resource.set_state_ok()
            resource.error_message = ""
            resource.error_traceback = ""
    finally:
        resource.save()


WALDUR_K8S_LABEL_SELECTOR = ",".join(f"{k}={v}" for k, v in WALDUR_K8S_LABELS.items())


@shared_task(name="waldur_marketplace_script.cleanup_orphaned_k8s_resources")
def cleanup_orphaned_k8s_resources():
    """Remove orphaned Kubernetes Jobs and ConfigMaps created by Waldur that are older than 1 hour."""
    if config.SCRIPT_RUN_MODE != DeploymentOptions.KUBERNETES.value:
        logger.debug(
            "SCRIPT_RUN_MODE is not kubernetes, skipping orphaned K8s resource cleanup"
        )
        return

    try:
        k8s_backend = kubernetes_backend.KubernetesBackend(
            kubeconfig_file_path=config.K8S_CONFIG_PATH
        )
    except KubernetesException:
        logger.warning(
            "Failed to initialize Kubernetes backend, skipping orphaned K8s resource cleanup",
            exc_info=True,
        )
        return

    namespace = config.K8S_NAMESPACE
    cutoff = timezone.now() - timezone.timedelta(hours=1)

    try:
        jobs = k8s_backend.list_k8s_jobs(
            namespace, label_selector=WALDUR_K8S_LABEL_SELECTOR
        )
    except KubernetesException:
        logger.warning(
            "Failed to list Kubernetes Jobs for orphan cleanup", exc_info=True
        )
        jobs = []

    for job in jobs:
        if job.metadata.creation_timestamp and job.metadata.creation_timestamp < cutoff:
            try:
                k8s_backend.delete_job_from_k8s(job.metadata.name, namespace)
                logger.info(
                    "Deleted orphaned Kubernetes Job %s (created at %s)",
                    job.metadata.name,
                    job.metadata.creation_timestamp,
                )
            except KubernetesException:
                logger.exception(
                    "Failed to delete orphaned Kubernetes Job %s", job.metadata.name
                )

    try:
        config_maps = k8s_backend.list_k8s_config_maps(
            namespace, label_selector=WALDUR_K8S_LABEL_SELECTOR
        )
    except KubernetesException:
        logger.warning(
            "Failed to list Kubernetes ConfigMaps for orphan cleanup", exc_info=True
        )
        config_maps = []

    for cm in config_maps:
        if cm.metadata.creation_timestamp and cm.metadata.creation_timestamp < cutoff:
            try:
                k8s_backend.delete_config_map_from_k8s(cm.metadata.name, namespace)
                logger.info(
                    "Deleted orphaned Kubernetes ConfigMap %s (created at %s)",
                    cm.metadata.name,
                    cm.metadata.creation_timestamp,
                )
            except KubernetesException:
                logger.exception(
                    "Failed to delete orphaned Kubernetes ConfigMap %s",
                    cm.metadata.name,
                )
