import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import CommandError

from waldur_core.core.utils import DryRunCommand
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace import models as marketplace_models, plugins
from waldur_mastermind.marketplace.utils import format_list
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import apps as slurm_apps
from waldur_mastermind.slurm_invoices import models as slurm_invoices_models

logger = logging.getLogger(__name__)


class ImportSLURMException(Exception):
    pass


def import_slurm_service_settings(default_customer, dry_run=False):
    """
    Import SLURM service settings as marketplace offerings.
    """
    service_type = slurm_apps.SlurmConfig.service_name
    category = marketplace_models.Category.objects.get(
        uuid=settings.WALDUR_MARKETPLACE_SLURM['CATEGORY_UUID']
    )

    slurm_offerings = marketplace_models.Offering.objects.filter(type=PLUGIN_NAME)
    front_settings = set(slurm_offerings.exclude(object_id=None).values_list('object_id', flat=True))

    back_settings = structure_models.ServiceSettings.objects.filter(type=service_type,
                                                                    state=structure_models.ServiceSettings.States.OK)
    missing_settings = back_settings.exclude(id__in=front_settings)

    if dry_run:
        logger.warning('SLURM service settings would be imported to marketplace. '
                       'IDs: %s.' % format_list(missing_settings))
        return missing_settings.count()

    for service_settings in missing_settings:
        offering = marketplace_models.Offering.objects.create(
            scope=service_settings,
            type=PLUGIN_NAME,
            name=service_settings.name,
            geolocations=service_settings.geolocations,
            customer=service_settings.customer or default_customer,
            category=category,
            shared=service_settings.shared,
            state=marketplace_models.Offering.States.ACTIVE,
        )

        components = plugins.manager.get_components(PLUGIN_NAME)

        for component_data in components:
            marketplace_models.OfferingComponent.objects.create(
                offering=offering,
                **component_data._asdict()
            )

        try:
            slurm_package = slurm_invoices_models.SlurmPackage.objects. \
                get(service_settings=service_settings)
            plan = marketplace_models.Plan.objects.create(
                offering=offering,
                scope=slurm_package,
                name=slurm_package.name)

            marketplace_models.PlanComponent.objects.create(
                plan=plan,
                component=offering.components.filter(type='cpu').get(),
                price=slurm_package.cpu_price)
            marketplace_models.PlanComponent.objects.create(
                plan=plan,
                component=offering.components.filter(type='gpu').get(),
                price=slurm_package.gpu_price)
            marketplace_models.PlanComponent.objects.create(
                plan=plan,
                component=offering.components.filter(type='ram').get(),
                price=slurm_package.ram_price)
        except slurm_invoices_models.SlurmPackage.DoesNotExist:
            logger.warning('Plan has not been created. Because SlurmPackage is not found. '
                           'Service settings UUID: %s.' % service_settings.uuid.hex)

    return missing_settings.count()


class Command(DryRunCommand):
    help = """Import SLURM service settings as marketplace offerings."""

    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)
        parser.add_argument('--customer', dest='customer_uuid', required=True,
                            help='Default customer argument is used for shared service setting.')

    def handle(self, customer_uuid, dry_run, *args, **options):
        try:
            customer = Customer.objects.get(uuid=customer_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A customer is not found.')

        try:
            offerings_counter = import_slurm_service_settings(customer, dry_run)
            self.stdout.write(self.style.SUCCESS('%s offerings have been created.' % offerings_counter))
        except ImportSLURMException as e:
            raise CommandError(e.message)
        except marketplace_models.Category.DoesNotExist:
            raise CommandError('Please ensure that WALDUR_MARKETPLACE_SLURM.CATEGORY_UUID '
                               'setting has valid value.')
