from django.core.management.base import BaseCommand

from waldur_core.structure.notifications import NOTIFICATIONS


class Command(BaseCommand):
    def handle(self, *args, **options):
        print('# Notifications', end='\n\n')
        for section in sorted(NOTIFICATIONS, key=lambda section: section['key']):
            for notification in sorted(
                section['items'], key=lambda section: section['path']
            ):
                print(f'## {section["key"]}.{notification["path"]}')
                print()
                print(notification["description"])
                print()
