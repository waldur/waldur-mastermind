from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class CostTrackingConfig(AppConfig):
    name = 'waldur_core.cost_tracking'
    verbose_name = 'Cost tracking'

    def ready(self):
        from waldur_core.cost_tracking import handlers
        from waldur_core.quotas import models as quotas_models
        from waldur_core.structure import models as structure_models

        PriceEstimate = self.get_model('PriceEstimate')

        for index, model in enumerate(PriceEstimate.get_estimated_models()):
            signals.pre_delete.connect(
                handlers.scope_deletion,
                sender=model,
                dispatch_uid='waldur_core.cost_tracking.handlers.scope_deletion_%s_%s' % (model.__name__, index),
            )

        for index, model in enumerate(structure_models.ResourceMixin.get_all_models()):
            signals.post_save.connect(
                handlers.resource_update,
                sender=model,
                dispatch_uid='waldur_core.cost_tracking.resource_update_%s_%s' % (model.__name__, index),
            )

        signals.post_save.connect(
            handlers.resource_quota_update,
            sender=quotas_models.Quota,
            dispatch_uid='waldur_core.cost_tracking.handlers.resource_quota_update',
        )
