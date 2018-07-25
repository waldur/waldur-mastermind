from __future__ import unicode_literals

from django.core.management.base import BaseCommand

from ... import utils


class Command(BaseCommand):
    help_text = 'Synchronize SAML2 identity providers.'

    def handle(self, *args, **options):
        utils.sync_providers()
        self.stdout.write('SAML2 providers have been successfully synchronized.')
