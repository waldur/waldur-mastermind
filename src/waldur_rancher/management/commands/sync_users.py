import logging

from django.core.management.base import BaseCommand

from waldur_rancher.utils import SyncUser

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = """Sync users from Waldur to Rancher."""

    def handle(self, *args, **options):
        def print_message(count, action, name='user'):
            if count == 1:
                self.stdout.write(
                    self.style.SUCCESS('%s %s has been %s.' % (count, name, action))
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS('%s %ss have been %s.' % (count, name, action))
                )

        result = SyncUser.run()

        for action in ['blocked', 'created', 'activated', 'updated']:
            print_message(result.get(action, 0), action)

        print_message(result.get('project roles deleted', 0), 'deleted', 'project role')
        print_message(result('project roles created', 0), 'created', 'project role')
