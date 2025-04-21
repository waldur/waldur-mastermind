import logging

import kubernetes as k8s
from django.core import exceptions as django_exceptions

from waldur_core.core import models as core_models
from waldur_kubernetes.backend import KubernetesBackend
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import (
    get_resource_state,
    import_current_usages,
)
from waldur_mastermind.marketplace_rancher import (
    MANAGED_RANCHER_PLUGIN,
    NODES_COMPONENT_TYPE,
)

logger = logging.getLogger(__name__)


def create_marketplace_resource_for_imported_cluster(
    sender, instance, offering=None, plan=None, **kwargs
):
    if not offering:
        # When cluster is imported directly (ie without marketplace),
        # marketplace resources are not created.
        return
    resource = marketplace_models.Resource(
        project=instance.project,
        state=get_resource_state(instance.state),
        name=instance.name,
        scope=instance,
        created=instance.created,
        plan=plan,
        offering=offering,
    )

    resource.init_cost()
    resource.save()


def update_node_usage(sender, instance, created=False, **kwargs):
    if not instance.tracker.has_changed("state"):
        return

    cluster = instance.cluster

    try:
        resource = marketplace_models.Resource.objects.get(scope=cluster)
    except django_exceptions.ObjectDoesNotExist:
        logger.debug(
            "Skipping node usage synchronization because this "
            "marketplace.Resource does not exist."
            "Cluster ID: %s",
            cluster.id,
        )
        return

    usage = cluster.node_set.filter(state=core_models.StateMixin.States.OK).count()

    resource.current_usages = {NODES_COMPONENT_TYPE: usage}
    resource.save(update_fields=["current_usages"])

    import_current_usages(resource)


def create_offering_user_for_rancher_user(sender, instance, created=False, **kwargs):
    if not created:
        return

    try:
        offering = marketplace_models.Offering.objects.get(scope=instance.settings)
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping Rancher user synchronization because offering is not found. "
            "Rancher settings ID: %s",
            instance.settings.id,
        )
        return

    marketplace_models.OfferingUser.objects.create(
        offering=offering,
        user=instance.user,
        username=instance.user.username,
    )


def drop_offering_user_for_rancher_user(sender, instance, **kwargs):
    try:
        offering = marketplace_models.Offering.objects.get(scope=instance.settings)
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping Rancher user synchronization because offering is not found. "
            "Rancher settings ID: %s",
            instance.settings.id,
        )
        return

    marketplace_models.OfferingUser.objects.filter(
        offering=offering, user=instance.user
    ).delete()


def update_argocd_secret_when_resource_options_changed(sender, instance, **kwargs):
    resource: marketplace_models.Resource = instance
    if not resource.tracker.has_changed("options"):
        return

    if resource.offering.type != MANAGED_RANCHER_PLUGIN:
        return

    if resource.state != marketplace_models.Resource.States.OK:
        return

    options = resource.options
    service_settings = resource.offering.secret_options
    kubeconfig_str = service_settings.options.get("argocd_k8s_kubeconfig")
    namespace = service_settings.options.get("argocd_k8s_namespace")
    secret_name = f"cluster-{resource.scope.uuid}"
    k8s_backend = KubernetesBackend(kubeconfig_str)
    try:
        k8s_backend.update_k8s_secret(secret_name, namespace, data=None, labels=options)
    except k8s.client.ApiException:
        logger.error("Failed to update the ArgoCD secret %s", secret_name)
        raise
