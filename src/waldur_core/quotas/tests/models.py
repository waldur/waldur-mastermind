from django.db import models as django_models

from waldur_core.core import models as core_models
from waldur_core.quotas import fields, models as quotas_models


class GrandparentModel(core_models.UuidMixin, quotas_models.QuotaModelMixin, core_models.DescendantMixin):
    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        regular_quota = fields.QuotaField()
        quota_with_default_limit = fields.QuotaField(default_limit=100)
        usage_aggregator_quota = fields.UsageAggregatorQuotaField(
            get_children=lambda scope: ChildModel.objects.filter(parent__parent=scope),
        )
        limit_aggregator_quota = fields.LimitAggregatorQuotaField(
            get_children=lambda scope: ChildModel.objects.filter(parent__parent=scope),
        )

    regular_quota = fields.QuotaLimitField(quota_field=Quotas.regular_quota)


class ParentModel(core_models.UuidMixin, quotas_models.QuotaModelMixin, core_models.DescendantMixin):
    parent = django_models.ForeignKey(GrandparentModel, related_name='children')

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        counter_quota = fields.CounterQuotaField(
            target_models=lambda: [ChildModel],
            path_to_scope='parent',
        )
        two_targets_counter_quota = fields.CounterQuotaField(
            target_models=lambda: [ChildModel, SecondChildModel],
            path_to_scope='parent',
        )
        delta_quota = fields.CounterQuotaField(
            target_models=lambda: [ChildModel],
            path_to_scope='parent',
            get_delta=lambda scope: 10
        )
        usage_aggregator_quota = fields.UsageAggregatorQuotaField(
            get_children=lambda scope: scope.children.all(),
        )
        limit_aggregator_quota = fields.LimitAggregatorQuotaField(
            get_children=lambda scope: scope.children.all(),
            default_limit=0,
        )
        second_usage_aggregator_quota = fields.UsageAggregatorQuotaField(
            get_children=lambda scope: scope.children.all(),
            child_quota_name='usage_aggregator_quota',
        )
        total_quota = fields.TotalQuotaField(
            target_models=lambda: [SecondChildModel],
            path_to_scope='parent',
            target_field='size',
        )

    def get_parents(self):
        return [self.parent]


class NonQuotaParentModel(core_models.UuidMixin, core_models.DescendantMixin):
    """ Allow to make sure that quotas propagation do not fail if quota scope has parent without quotas """
    pass


class ChildModel(core_models.UuidMixin, quotas_models.QuotaModelMixin, core_models.DescendantMixin):
    parent = django_models.ForeignKey(ParentModel, related_name='children')
    non_quota_parent = django_models.ForeignKey(NonQuotaParentModel, related_name='children', blank=True, null=True)

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        regular_quota = fields.QuotaField()
        usage_aggregator_quota = fields.QuotaField()  # this quota is aggregated by parent and grandparent
        limit_aggregator_quota = fields.QuotaField(default_limit=0)  # this quota is aggregated by parent and grandparent

    def get_parents(self):
        if self.non_quota_parent:
            return [self.parent, self.non_quota_parent]
        return [self.parent]


class SecondChildModel(core_models.UuidMixin, quotas_models.QuotaModelMixin, core_models.DescendantMixin):
    parent = django_models.ForeignKey(ParentModel, related_name='second_children')
    size = django_models.IntegerField(default=0)

    def get_parents(self):
        return [self.parent]
