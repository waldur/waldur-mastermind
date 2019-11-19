from waldur_rancher import models as rancher_models

from . import utils


def update_node_usage(sender, instance, created=False, **kwargs):
    if created:
        return

    utils.create_usage(instance)


def create_invoice_item_if_component_usage_has_been_created(sender, instance, created=False, **kwargs):
    component_usage = instance

    if not component_usage.tracker.has_changed('usage'):
        return

    if not isinstance(component_usage.resource.scope, rancher_models.Cluster):
        return

    utils.component_usage_register(component_usage)
