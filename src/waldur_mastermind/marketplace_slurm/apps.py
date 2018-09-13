from django.apps import AppConfig
from django.db.models import signals
from waldur_mastermind.marketplace_slurm import COMPONENTS


class MarketplaceSlurmConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_slurm'
    verbose_name = 'Marketplace SLURM'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
        from waldur_core.structure import models as structure_models

        from . import handlers, processor

        signals.post_save.connect(
            handlers.create_slurm_package,
            sender=marketplace_models.PlanComponent,
            dispatch_uid='waldur_mastermind.marketpace_slurm.create_slurm_package',
        )

        manager.register(PLUGIN_NAME, processor.process_slurm,
                         scope_model=structure_models.ServiceSettings,
                         components=COMPONENTS)
