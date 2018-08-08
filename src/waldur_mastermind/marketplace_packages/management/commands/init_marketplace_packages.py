from django.core.management.base import BaseCommand
from waldur_mastermind.marketplace_packages import utils


class Command(BaseCommand):
    help = 'Init marketplace offerings for OpenStack package templates.'

    def handle(self, *args, **options):
        utils.create_missing_offerings()
