import sys

import yaml
from django.core.management.base import BaseCommand

from waldur_auth_social.models import IdentityProvider
from waldur_auth_social.serializers import IdentityProviderSerializer


class Command(BaseCommand):
    help = "Export OIDC auth configuration as YAML format"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            '-o',
            '--output',
            dest='output',
            default=None,
            help='Specifies file to which the output is written. The output will be printed to stdout by default.',
        )

    def handle(self, *args, **options):
        providers = IdentityProvider.objects.all()
        serializer = IdentityProviderSerializer(instance=providers, many=True)
        if options['output'] is None:
            self.stdout.write(yaml.safe_dump(dict(x) for x in serializer.data))
        else:
            with open(options['output'] or sys.stdout, 'w') as output_file:
                yaml.safe_dump((dict(x) for x in serializer.data), output_file)
