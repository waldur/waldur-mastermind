import logging
from typing import cast
from uuid import uuid4

import kubernetes as k8s
from django.utils import timezone
from model_utils.tracker import FieldInstanceTracker

from waldur_kubernetes.backend import KubernetesBackend
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.invoices.registrators import RegistrationManager
from waldur_mastermind.invoices.utils import get_current_month_end
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator
from waldur_mastermind.marketplace.utils import (
    get_resource_state,
    serialize_resource_limit_period,
)
from waldur_mastermind.marketplace_openstack import TENANT_TYPE
from waldur_mastermind.marketplace_rancher import MANAGED_RANCHER_PLUGIN
from waldur_mastermind.marketplace_rancher.const import OS_LB_PREFIX
from waldur_openstack import models as openstack_models
from waldur_rancher.exceptions import RancherException
from waldur_rancher.models import Cluster, ClusterPublicIP, RancherUser

logger = logging.getLogger(__name__)


def create_marketplace_resource_for_imported_cluster(
    sender, instance: Cluster, offering=None, plan=None, **kwargs
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
    resource = instance
    tracker = cast(FieldInstanceTracker, resource.tracker)
    if not tracker.has_changed("options"):
        return

    if resource.offering.type != MANAGED_RANCHER_PLUGIN:
        return

    if resource.state != ResourceStates.OK:
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

    cluster_resource = cast(marketplace_models.Resource, resource.scope)
    cluster = cast(Cluster, cluster_resource.scope)
    secret_name = f"cluster-{cluster.uuid}"

    k8s_backend = KubernetesBackend(kubeconfig_str=kubeconfig_str)
    try:
        k8s_backend.update_k8s_secret(secret_name, namespace, data=None, labels=options)
    except k8s.client.ApiException:
        logger.error("Failed to update the ArgoCD secret %s", secret_name)
        raise


def copy_invoice_items_when_cluster_is_provisioned(
    sender, instance: marketplace_models.Resource, **kwargs
):
    resource = instance
    tracker = cast(FieldInstanceTracker, resource.tracker)
    if not tracker.has_changed("state"):
        return

    if resource.offering.type != MANAGED_RANCHER_PLUGIN:
        return

    if resource.state != ResourceStates.OK:
        return

    cluster_resource = cast(marketplace_models.Resource, resource.scope)
    cluster = cast(Cluster, cluster_resource.scope)
    if not cluster:
        return

    now = timezone.now()
    end = get_current_month_end()

    source_items = InvoiceItem.objects.filter(
        resource__object_id__in=cluster.linked_tenant_ids,
        resource__offering__type=TENANT_TYPE,
        invoice__year=now.year,
        invoice__month=now.month,
    )

    invoice, _ = RegistrationManager.get_or_create_invoice(cluster.customer, now)

    # Copy invoice items from linked tenants to the new cluster resource.
    for invoice_item in source_items:
        invoice_item.pk = None
        invoice_item.uuid = uuid4()
        invoice_item.resource = resource
        invoice_item.invoice = invoice
        invoice_item.project = cluster.project
        invoice_item.project_name = cluster.project.name
        invoice_item.project_uuid = cluster.project.uuid.hex
        invoice_item.start = now
        try:
            quantity = invoice_item.details["resource_limit_periods"][0]["quantity"]
            invoice_item.details["resource_limit_periods"] = [
                serialize_resource_limit_period(
                    {"start": now, "end": end, "quantity": quantity}
                )
            ]
            total_quantity = MarketplaceRegistrator.get_total_quantity(
                invoice_item.unit, quantity, now, end
            )
            invoice_item.quantity = total_quantity
        except (KeyError, IndexError):
            logger.debug(
                "Failed to copy resource limit periods for invoice item %s",
                invoice_item,
            )
        invoice_item.save()

    if not resource.plan:
        logger.debug(
            "Skipping invoice item creation for resource %s because plan is not set.",
            resource,
        )
        return

    # Create aggregated invoice items for each component of the plan.
    for plan_component in resource.plan.components.all():
        offering_component = plan_component.component
        if not offering_component:
            logger.debug(
                "Skipping invoice item creation for resource %s because offering component is not set.",
                resource,
            )
            continue
        component_items = [
            item
            for item in source_items
            if item.details.get("offering_component_type") == offering_component.type
        ]
        if not component_items:
            logger.debug(
                "Skipping invoice item creation for resource %s because no source items are found for offering component %s.",
                resource,
                offering_component.type,
            )
            continue
        details = MarketplaceRegistrator.get_component_details(resource, plan_component)
        try:
            quantity = sum(
                invoice_item.details["resource_limit_periods"][0]["quantity"]
                for invoice_item in component_items
            )
            details["resource_limit_periods"] = [
                serialize_resource_limit_period(
                    {"start": now, "end": end, "quantity": quantity}
                )
            ]
            total_quantity = MarketplaceRegistrator.get_total_quantity(
                plan_component.plan.unit, quantity, now, end
            )
        except (KeyError, IndexError):
            logger.debug(
                "Failed to copy resource limit periods for plan component %s",
                plan_component,
            )
            continue

        InvoiceItem.objects.create(
            invoice=invoice,
            resource=resource,
            project=cluster.project,
            project_name=cluster.project.name,
            project_uuid=cluster.project.uuid.hex,
            start=cluster.created,
            unit_price=plan_component.price,
            unit=resource.plan.unit,
            article_code=offering_component.article_code,
            measured_unit=offering_component.measured_unit,
            details=details,
            quantity=total_quantity,
        )


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

    cluster_resource = resource.scope

    if not cluster_resource:
        logger.warning(
            "Skipping creation of public cluster IP for floating IP %s because "
            "cluster resource is not found.",
            floating_ip,
        )
        return

    cluster = cast(Cluster, cluster_resource.scope)

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
