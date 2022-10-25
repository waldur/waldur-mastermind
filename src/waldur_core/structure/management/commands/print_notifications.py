from django.conf import settings
from django.core.management.base import BaseCommand

from waldur_core.structure.notifications import NOTIFICATIONS


class Command(BaseCommand):
    def handle(self, *args, **options):
        print('# Notifications', end='\n\n')
        for section in sorted(NOTIFICATIONS, key=lambda section: section['key']):
            for app in settings.INSTALLED_APPS:
                plugin = app.split('.')[1] if len(app.split('.')) == 2 else app
                if section["key"] == plugin or f"waldur_{section['key']}" == plugin:
                    print(f"# {app.upper()}")
                    print()
            for notification in sorted(
                section['items'], key=lambda section: section['path']
            ):
                print(f'## {section["key"]}.{notification["path"]}')
                print()
                print(notification["description"])
                print()
                print("### Templates")
                for template in notification['templates']:
                    print(f"*{section['key']}/{template.path}*")
                print()
