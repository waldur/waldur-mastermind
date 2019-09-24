from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace.models import Offering, Plan
from waldur_mastermind.marketplace_openstack import PACKAGE_TYPE

from ... import utils


class Command(BaseCommand):
    help = """Merge marketplace plans related to OpenStack tenant offering."""

    def add_arguments(self, parser):
        parser.add_argument('--offering', dest='offering_uuid', required=True,
                            help='UUID of marketplace offering for OpenStack tenant provisioning.')
        parser.add_argument('--plan', dest='plan_uuid', required=True,
                            help='UUID of example marketplace plan related to the same offering.')

    def handle(self, *args, **options):
        offering_uuid = options['offering_uuid']
        plan_uuid = options['plan_uuid']

        try:
            offering = Offering.objects.get(uuid=offering_uuid)
        except ObjectDoesNotExist:
            raise CommandError('Offering with given UUID is not found.')

        if offering.type != PACKAGE_TYPE:
            raise CommandError('Offering is not related to OpenStack tenants.')

        try:
            plan = Plan.objects.get(uuid=plan_uuid)
        except ObjectDoesNotExist:
            raise CommandError('Plan with given UUID is not found.')

        if plan.offering != offering:
            raise CommandError('Plan is related to another offering.')

        utils.merge_plans(offering, plan)
