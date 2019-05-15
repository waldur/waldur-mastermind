from django.db.models import F, signals

from waldur_core.quotas import models, utils, fields
from waldur_core.quotas.exceptions import CreationConditionFailedQuotaError


# XXX: rewrite global quotas
def create_global_quotas(**kwargs):
    for model in utils.get_models_with_quotas():
        if hasattr(model, 'GLOBAL_COUNT_QUOTA_NAME'):
            models.Quota.objects.get_or_create(name=getattr(model, 'GLOBAL_COUNT_QUOTA_NAME'))


def increase_global_quota(sender, instance=None, created=False, **kwargs):
    if created and hasattr(sender, 'GLOBAL_COUNT_QUOTA_NAME'):
        name = getattr(sender, 'GLOBAL_COUNT_QUOTA_NAME')
        models.Quota.objects.filter(name=name).update(usage=F('usage') + 1)


def decrease_global_quota(sender, **kwargs):
    if hasattr(sender, 'GLOBAL_COUNT_QUOTA_NAME'):
        name = getattr(sender, 'GLOBAL_COUNT_QUOTA_NAME')
        models.Quota.objects.filter(name=name).update(usage=F('usage') - 1)


# new quotas

def init_quotas(sender, instance, created=False, **kwargs):
    """ Initialize new instances quotas """
    if not created:
        return
    for field in sender.get_quotas_fields():
        try:
            field.get_or_create_quota(scope=instance)
        except CreationConditionFailedQuotaError:
            pass


def count_quota_handler_factory(count_quota_field):
    """ Creates handler that will recalculate count_quota on creation/deletion """

    def recalculate_count_quota(sender, instance, **kwargs):
        signal = kwargs['signal']
        if signal == signals.post_save and kwargs.get('created'):
            count_quota_field.add_usage(instance, delta=1)
        elif signal == signals.post_delete:
            count_quota_field.add_usage(instance, delta=-1)

    return recalculate_count_quota


def handle_aggregated_quotas(sender, instance, **kwargs):
    """ Call aggregated quotas fields update methods """
    quota = instance
    # aggregation is not supported for global quotas.
    if quota.scope is None:
        return
    quota_field = quota.get_field()
    # usage aggregation should not count another usage aggregator field to avoid calls duplication.
    if isinstance(quota_field, fields.UsageAggregatorQuotaField) or quota_field is None:
        return
    signal = kwargs['signal']
    for aggregator_quota in quota_field.get_aggregator_quotas(quota):
        field = aggregator_quota.get_field()
        if signal == signals.post_save:
            field.post_child_quota_save(aggregator_quota.scope, child_quota=quota, created=kwargs.get('created'))
        elif signal == signals.pre_delete:
            field.pre_child_quota_delete(aggregator_quota.scope, child_quota=quota)
