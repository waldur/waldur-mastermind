from django.core.exceptions import ObjectDoesNotExist
from django.db.models import signals

from waldur_core.core.models import DescendantMixin
from waldur_core.quotas import fields
from waldur_core.quotas.models import QuotaLimit, QuotaModelMixin, QuotaUsage
from waldur_core.structure import models as structure_models

# new quotas


def count_quota_handler_factory(count_quota_field):
    """Creates handler that will recalculate count_quota on creation/deletion"""

    def recalculate_count_quota(sender, instance, **kwargs):
        """Recalculate count quota when an instance is created or deleted."""
        signal = kwargs["signal"]
        if signal == signals.post_save and kwargs.get("created"):
            count_quota_field.add_usage(instance, delta=1)
        elif signal == signals.post_delete:
            count_quota_field.add_usage(instance, delta=-1)

    return recalculate_count_quota


def get_ancestors(scope, max_depth=10, current_depth=0):
    """Get all unique instance ancestors with recursion depth limit"""
    # Prevent infinite recursion
    if current_depth >= max_depth:
        return []

    try:
        # Check if the scope is being deleted or is invalid
        if not scope or not hasattr(scope, "get_parents"):
            return []
        ancestors = list(scope.get_parents())
    except (AttributeError, KeyError, ObjectDoesNotExist):
        # Handle case where relationships are missing during deletion
        return []

    ancestor_unique_attributes = set([(a.__class__, a.id) for a in ancestors])
    ancestors_with_parents = [a for a in ancestors if isinstance(a, DescendantMixin)]
    for ancestor in ancestors_with_parents:
        try:
            for parent in get_ancestors(ancestor, max_depth, current_depth + 1):
                if (parent.__class__, parent.id) not in ancestor_unique_attributes:
                    ancestors.append(parent)
                    ancestor_unique_attributes.add((parent.__class__, parent.id))
        except (AttributeError, KeyError, ObjectDoesNotExist):
            # Skip ancestors that can't be traversed due to missing relationships
            continue
    return ancestors


def get_field(quota):
    """Get the quota field for a given quota."""
    fields = quota.scope.get_quotas_fields()
    try:
        return next(f for f in fields if f.name == quota.name)
    except StopIteration:
        return


def handle_aggregated_quotas(sender, instance: QuotaUsage, **kwargs):
    """Call aggregated quotas fields update methods"""
    quota = instance

    # Skip processing if the scope is being deleted or doesn't exist
    try:
        if not quota.scope or not hasattr(quota, "scope"):
            return
    except (AttributeError, ObjectDoesNotExist):
        return

    quota_field = get_field(quota)
    # usage aggregation should not count another usage aggregator field to avoid calls duplication.
    if isinstance(quota_field, fields.UsageAggregatorQuotaField) or quota_field is None:
        return
    signal = kwargs["signal"]

    # During bulk deletion (like project deletion), skip aggregation to avoid timeout
    # The parent quotas will be deleted anyway
    if signal == signals.pre_delete and getattr(quota.scope, "_deleting", False):
        return

    ancestors = {}
    if isinstance(quota.scope, DescendantMixin):
        # We need to use set in order to eliminate duplicates.
        # Consider, for example, two ways of traversing from resource to customer:
        # resource -> project -> customer
        # resource -> service -> customer
        try:
            ancestors = {
                a for a in get_ancestors(quota.scope) if isinstance(a, QuotaModelMixin)
            }
        except (AttributeError, KeyError, ObjectDoesNotExist):
            # Handle case where relationships are missing during deletion
            ancestors = set()
    aggregator_quotas = []
    for ancestor in ancestors:
        try:
            for ancestor_quota_field in ancestor.get_quotas_fields(
                field_class=fields.UsageAggregatorQuotaField
            ):
                if ancestor_quota_field.get_child_quota_name() == quota.name:
                    aggregator_quotas.append((ancestor, ancestor_quota_field))
        except (AttributeError, ObjectDoesNotExist):
            # Skip ancestors that are being deleted
            continue

    for ancestor, field in aggregator_quotas:
        try:
            if signal == signals.post_save:
                delta = quota.delta
            elif signal == signals.pre_delete:
                delta = -quota.delta
            ancestor.add_quota_usage(field.name, delta)
        except (AttributeError, ObjectDoesNotExist):
            # Skip if ancestor is being deleted
            continue


def delete_quotas_when_model_is_deleted(sender, instance, **kwargs):
    """Delete all quotas related to a model when it is deleted."""
    QuotaLimit.objects.filter(scope=instance).delete()
    QuotaUsage.objects.filter(scope=instance).delete()


def projects_customer_has_been_changed(
    sender, project, old_customer, new_customer, created=False, **kwargs
):
    """Recalculate quotas when a project's customer has been changed."""

    def recalculate_quotas(field_class):
        for counter_field in structure_models.Customer.get_quotas_fields(
            field_class=field_class
        ):
            for customer in [old_customer, new_customer]:
                counter_field.recalculate(scope=customer)

    recalculate_quotas(fields.CounterQuotaField)
    recalculate_quotas(fields.UsageAggregatorQuotaField)
