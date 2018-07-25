import os

from django.conf import settings
from django.core.management.base import BaseCommand

from waldur_core.core.docs import ApiDocs


class Command(BaseCommand):
    help = "Generate RST docs for DRF API"

    def add_arguments(self, parser):
        parser.add_argument(
            '--store', '-s', action='store', dest='path',
            default='docs/drfapi', help='Where to store docs.'
        )
        parser.add_argument('args', metavar='app_label', nargs='*', help='Application label.')

    def handle(self, *app_labels, **options):
        path = options.get('path', 'docs/drfapi')
        path = path if path.startswith('/') else os.path.join(settings.BASE_DIR, path)

        if not os.path.isdir(path):
            os.makedirs(path)
        else:
            for f in os.listdir(path):
                if f.endswith(".rst"):
                    os.remove(os.path.join(path, f))

        self.stdout.write(self.style.MIGRATE_HEADING('Gather endpoints info'))
        docs = ApiDocs(apps=app_labels)
        self.stdout.write(self.style.MIGRATE_HEADING('Write RST docs'))
        docs.generate(path)
