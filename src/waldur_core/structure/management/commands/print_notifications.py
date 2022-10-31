from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import get_template

from waldur_core.server import settings as core_settings
from waldur_core.structure.notifications import NOTIFICATIONS

TAB_OF_4 = ' ' * 4

CUSTOM_LOADER_SETTING = (
    core_settings.ADMIN_TEMPLATE_LOADERS,
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
)


class Command(BaseCommand):
    def handle(self, *args, **options):
        settings.TEMPLATES[0]['OPTIONS']['loaders'] = CUSTOM_LOADER_SETTING
        print('# Notifications', end='\n\n')
        for section in sorted(NOTIFICATIONS, key=lambda section: section['key']):
            for app in settings.INSTALLED_APPS:
                plugin = app.split('.')[1] if len(app.split('.')) == 2 else app
                if section["key"] == plugin or f"waldur_{section['key']}" == plugin:
                    print(f"## {app.upper()}", end='\n\n')
            for notification in sorted(
                section['items'], key=lambda section: section['path']
            ):
                print(f'### {section["key"]}.{notification["path"]}', end='\n\n')
                print(notification["description"], end='\n\n')
                print("#### Templates", end='\n\n')
                for template in notification['templates']:
                    template_path = f"{section['key']}/{template.path}"
                    print(f'=== "{template_path}"', end='\n\n')
                    print(f"{TAB_OF_4}```")
                    source = get_template(template_path).template.source
                    source = source.replace('\n', f'\n{TAB_OF_4}')
                    print(f"{TAB_OF_4}{source}")
                    print(f"{TAB_OF_4}```")
                print()
