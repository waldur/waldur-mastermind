import logging
from typing import cast

from model_utils.tracker import FieldInstanceTracker

from waldur_kubernetes.backend import KubernetesBackend
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.utils import get_resource_state
from waldur_mastermind.marketplace_rancher.const import (
    DEPLOYMENT_MODE_MANAGED,
    DEPLOYMENT_MODE_SELF_MANAGED,
    OS_LB_PREFIX,
)
from waldur_openstack import models as openstack_models
from waldur_rancher.exceptions import RancherException
from waldur_rancher.models import Cluster, ClusterPublicIP, RancherUser

logger = logging.getLogger(__name__)


def create_marketplace_resource_for_imported_cluster(
    sender, instance: Cluster, offering=None, plan=None, **kwargs
):
    """Create marketplace resource when Rancher cluster is imported from external system."""
    if not offering:
        # When cluster is imported directly (ie without marketplace),
        # marketplace resources are not created.
        return

    resource_params = {
        "name": instance.name,
        "scope": instance,
    }

    resource = marketplace_models.Resource(
        project=instance.project,
        state=get_resource_state(instance.state),
        created=instance.created,
        plan=plan,
        offering=offering,
        **resource_params,
    )

    resource.init_cost()
    resource.save()


def create_offering_user_for_rancher_user(
    sender, instance: RancherUser, created=False, **kwargs
):
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


def drop_offering_user_for_rancher_user(sender, instance: RancherUser, **kwargs):
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


def update_argocd_secret_when_resource_options_changed(
    sender, instance: marketplace_models.Resource, **kwargs
):
    # Lazy import: keep the kubernetes SDK out of Django startup.
    # See CLAUDE.md, "Lazy imports for heavy optional backends".
    import kubernetes as k8s

    resource = instance
    tracker = cast(FieldInstanceTracker, resource.tracker)
    if not tracker.has_changed("options"):
        return

    if resource.state != ResourceStates.OK:
        return

    deployment_mode = resource.offering.plugin_options.get(
        "deployment_mode", DEPLOYMENT_MODE_SELF_MANAGED
    )
    if deployment_mode != DEPLOYMENT_MODE_MANAGED:
        return

    options = cast(dict, resource.options)
    secret_options = cast(dict, resource.offering.secret_options)

    kubeconfig_str = secret_options.get("argocd_k8s_kubeconfig")
    namespace = secret_options.get("argocd_k8s_namespace")
    if not kubeconfig_str or not namespace:
        raise RancherException(
            'Failed to update the ArgoCD secret because "argocd_k8s_kubeconfig" or '
            '"argocd_k8s_namespace" is not set in the offering.'
        )

    cluster = cast(Cluster, resource.scope)
    secret_name = f"cluster-{cluster.uuid}"

    k8s_backend = KubernetesBackend(kubeconfig_str=kubeconfig_str)
    try:
        k8s_backend.update_k8s_secret(secret_name, namespace, data=None, labels=options)
    except k8s.client.ApiException:
        logger.error("Failed to update the ArgoCD secret %s", secret_name)
        raise


def create_public_cluster_ip_for_floating_ip(
    sender, instance: openstack_models.FloatingIP, created=False, **kwargs
):
    floating_ip = instance
    tracker = cast(FieldInstanceTracker, floating_ip.tracker)

    if not (
        tracker.has_changed("runtime_state") and floating_ip.runtime_state == "ACTIVE"
    ):
        return

    floating_ip_instance = floating_ip.port.instance if floating_ip.port else None

    if not floating_ip_instance:
        logger.warning(
            "Skipping creation of public cluster IP for floating IP %s because "
            "floating IP instance is not found.",
            floating_ip,
        )
        return

    # Naming convention for Managed Rancher load balancer IPs
    # See ManagedRancherCreateProcessor.create_load_balancers method for details
    if not floating_ip_instance.name.startswith(OS_LB_PREFIX):
        return

    resource_slug = floating_ip_instance.name.replace(OS_LB_PREFIX, "", 1)

    resource = marketplace_models.Resource.objects.filter(slug=resource_slug).first()

    if not resource:
        logger.warning(
            "Skipping creation of public cluster IP for floating IP %s because "
            "resource with slug %s is not found.",
            floating_ip,
            resource_slug,
        )
        return

    cluster = cast(Cluster, resource.scope)

    if not cluster:
        logger.warning(
            "Skipping creation of public cluster IP for floating IP %s because "
            "cluster is not found.",
            floating_ip,
        )
        return

    ClusterPublicIP.objects.get_or_create(
        cluster=cluster,
        floating_ip=floating_ip,
    )
