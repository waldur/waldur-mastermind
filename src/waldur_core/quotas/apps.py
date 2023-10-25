from django.apps import AppConfig
from django.db.models import signals


class QuotasConfig(AppConfig):
    """
    Quotas - objects resource limits and their usage.
    Quotas limits can be editable by users.
    """

    name = 'waldur_core.quotas'
    verbose_name = 'Quotas'

    def ready(self):
        from waldur_core.quotas import handlers, utils
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals

        QuotaUsage = self.get_model('QuotaUsage')

        # new quotas
        from waldur_core.quotas import fields

        for _, model in enumerate(utils.get_models_with_quotas()):
            # Counter quota signals
            # How it works:
            # Each counter quota field has list of target models. Change of target model should increase or decrease
            # counter quota. So we connect generated handler to each of target models.
            for counter_field in model.get_quotas_fields(
                field_class=fields.CounterQuotaField
            ):
                self.register_counter_field_signals(model, counter_field)

            signals.pre_delete.connect(
                handlers.delete_quotas_when_model_is_deleted,
                sender=model,
                dispatch_uid='waldur_core.quotas.delete_quotas_when_model_is_deleted',
            )

        # Aggregator quotas signals
        signals.post_save.connect(
            handlers.handle_aggregated_quotas,
            sender=QuotaUsage,
            dispatch_uid='waldur_core.quotas.handle_aggregated_quotas_post_save',
        )

        signals.pre_delete.connect(
            handlers.handle_aggregated_quotas,
            sender=QuotaUsage,
            dispatch_uid='waldur_core.quotas.handle_aggregated_quotas_pre_delete',
        )

        structure_signals.project_moved.connect(
            handlers.projects_customer_has_been_changed,
            sender=structure_models.Project,
            dispatch_uid='waldur_core.quotas.projects_customer_has_been_changed',
        )

    @staticmethod
    def register_counter_field_signals(model, counter_field):
        from waldur_core.quotas import handlers

        for target_model_index, target_model in enumerate(counter_field.target_models):
            signals.post_save.connect(
                handlers.count_quota_handler_factory(counter_field),
                sender=target_model,
                weak=False,  # saves handler from garbage collector
                dispatch_uid='waldur_core.quotas.increase_counter_quota_%s_%s_%s_%s_%s'
                % (
                    model.__name__,
                    model._meta.app_label,
                    counter_field.name,
                    target_model.__name__,
                    target_model_index,
                ),
            )

            signals.post_delete.connect(
                handlers.count_quota_handler_factory(counter_field),
                sender=target_model,
                weak=False,  # saves handler from garbage collector
                dispatch_uid='waldur_core.quotas.decrease_counter_quota_%s_%s_%s_%s_%s'
                % (
                    model.__name__,
                    model._meta.app_label,
                    counter_field.name,
                    target_model.__name__,
                    target_model_index,
                ),
            )
