import datetime
import logging

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import CommandError
from django.db import transaction

from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import format_list
from waldur_slurm import models as slurm_models

logger = logging.getLogger(__name__)


class ImportAllocationException(Exception):
    pass


@transaction.atomic()
def import_allocation(dry_run=False):
    ct = ContentType.objects.get_for_model(slurm_models.Allocation)
    exist_ids = marketplace_models.Resource.objects.filter(content_type=ct).values_list('object_id', flat=True)
    missing_allocations = slurm_models.Allocation.objects.exclude(id__in=exist_ids)

    if dry_run:
        logger.warning('Allocations would be imported to marketplace. '
                       'IDs: %s.' % format_list(missing_allocations))
        return missing_allocations.count()

    for allocation in missing_allocations:
        offering = marketplace_models.Offering.objects.filter(scope=allocation.service_settings).first()
        component_cpu = offering.components.get(type='cpu')
        component_gpu = offering.components.get(type='gpu')
        component_ram = offering.components.get(type='ram')

        try:
            plan = marketplace_models.Plan.objects.get(offering=offering)
        except marketplace_models.Plan.DoesNotExist:
            logger.warning('Resource has not been created. Because Plan is not found. '
                           'Offering UUID: %s.' % offering.uuid.hex)
            continue

        state = marketplace_models.Resource.States.OK if allocation.is_active \
            else marketplace_models.Resource.States.TERMINATED
        resource = marketplace_models.Resource.objects.create(
            content_type=ct,
            object_id=allocation.id,
            state=state,
            project=allocation.service_project_link.project,
            offering=offering,
            created=allocation.created,
            plan=plan,
            limits={'deposit_limit': int(allocation.deposit_limit)}
        )

        marketplace_models.ComponentQuota.objects.create(
            resource=resource,
            limit=allocation.cpu_limit,
            component=component_cpu)
        marketplace_models.ComponentQuota.objects.create(
            resource=resource,
            limit=allocation.gpu_limit,
            component=component_gpu)
        marketplace_models.ComponentQuota.objects.create(
            resource=resource,
            limit=allocation.ram_limit,
            component=component_ram)

        resource_plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=allocation.created,
            end=None,
        )

        for allocation_usage in allocation.allocationusage_set.all():
            if allocation_usage.year == allocation.created.year and \
                    allocation_usage.month == allocation.created.month:
                day = allocation.created.day
            else:
                day = 1

            date = datetime.date(allocation_usage.year, allocation_usage.month, day)
            marketplace_models.ComponentUsage.objects.create(
                resource=resource,
                component=component_cpu,
                usage=allocation_usage.cpu_usage,
                date=date,
                plan_period=resource_plan_period
            )
            marketplace_models.ComponentUsage.objects.create(
                resource=resource,
                component=component_gpu,
                usage=allocation_usage.gpu_usage,
                date=date,
                plan_period=resource_plan_period
            )
            marketplace_models.ComponentUsage.objects.create(
                resource=resource,
                component=component_ram,
                usage=allocation_usage.ram_usage,
                date=date,
                plan_period=resource_plan_period
            )
    return missing_allocations.count()


class Command(DryRunCommand):
    help = """Import existing SLURM allocations as marketplace resources.
    It is expected that offerings for SLURM service settings are imported before this command is ran."""

    def handle(self, dry_run, *args, **options):
        try:
            allocation_counter = import_allocation(dry_run)
            self.stdout.write(self.style.SUCCESS('%s resources have been created.' % allocation_counter))
        except ImportAllocationException as e:
            raise CommandError(e.message)
