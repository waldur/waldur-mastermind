import logging

from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_mastermind.slurm_invoices import models as slurm_invoices_models
from waldur_slurm.apps import SlurmConfig
from waldur_mastermind.marketplace_slurm import COMPONENTS

logger = logging.getLogger(__name__)


def create_slurm_package(sender, instance, created=False, **kwargs):
    plan = instance.plan

    if not created:
        return

    if plan.offering.type != PLUGIN_NAME:
        return

    if not isinstance(plan.offering.scope, structure_models.ServiceSettings):
        logger.warning('Skipping plan synchronization because offering scope is not service settings. '
                       'Plan ID: %s', plan.id)
        return

    if plan.offering.scope.type != SlurmConfig.service_name:
        logger.warning('Skipping plan synchronization because service settings type is not SLURM. '
                       'Plan ID: %s', plan.id)
        return

    if {c.type for c in plan.components.all()} != set(COMPONENTS.keys()):
        return

    with transaction.atomic():
        slurm_package = slurm_invoices_models.SlurmPackage.objects.create(
            service_settings=plan.offering.scope,
            name=plan.name,
            cpu_price=plan.components.get(type='cpu').price,
            gpu_price=plan.components.get(type='gpu').price,
            ram_price=plan.components.get(type='ram').price,
        )
        plan.scope = slurm_package
        plan.save()
