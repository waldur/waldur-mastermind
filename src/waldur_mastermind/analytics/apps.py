from django.apps import AppConfig
from django.db.models import signals


class AnalyticsConfig(AppConfig):
    name = 'waldur_mastermind.analytics'
    verbose_name = 'Analytics'

    def ready(self):
        from waldur_core.structure.models import ResourceMixin
        from . import handlers

        for index, model in enumerate(ResourceMixin.get_all_models()):
            signals.post_save.connect(
                handlers.log_resource_created,
                sender=model,
                dispatch_uid=('waldur_mastermind.analytics.handlers.'
                              'log_resource_created_{}_{}'.format(model.__name__, index)),
            )

            signals.post_delete.connect(
                handlers.log_resource_deleted,
                sender=model,
                dispatch_uid=('waldur_mastermind.analytics.handlers.'
                              'log_resource_deleted_{}_{}'.format(model.__name__, index)),
            )
