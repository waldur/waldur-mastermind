from django.apps import AppConfig


class MarketplaceMetricsConfig(AppConfig):
    name = "waldur_mastermind.marketplace_metrics"
    verbose_name = "Marketplace custom metrics"

    def ready(self):
        from django.db.models.signals import post_migrate

        from waldur_core.core import signals as core_signals
        from waldur_core.structure import serializers as structure_serializers

        from . import handlers

        post_migrate.connect(
            handlers.create_default_retention_policy,
            sender=self,
            dispatch_uid="waldur_mastermind.marketplace_metrics.create_default_retention_policy",
        )
        core_signals.pre_serializer_fields.connect(
            sender=structure_serializers.ProjectSerializer,
            receiver=handlers.add_has_metrics,
            dispatch_uid="waldur_mastermind.marketplace_metrics.add_has_metrics",
        )
