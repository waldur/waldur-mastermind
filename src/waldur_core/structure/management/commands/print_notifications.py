import re

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import engines

from waldur_core.structure.notifications import NOTIFICATIONS

TAB_OF_4 = ' ' * 4

CUSTOM_LOADER_SETTING = (
    'admin_tools.template_loaders.Loader',
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
)


class Command(BaseCommand):
    def handle(self, *args, **options):
        file_engine = engines.all()
        # reset loaders to use only filesystem based
        file_engine[0].engine.loaders = CUSTOM_LOADER_SETTING
        # reset cached_property
        del file_engine[0].engine.__dict__["template_loaders"]

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
                    source = file_engine[0].get_template(template_path).template.source
                    source = source.replace('\n', f'\n{TAB_OF_4}')
                    source = re.sub(' +\n', '\n', source)
                    source = source.rstrip()
                    print(f"{TAB_OF_4}{source}", end='\n')
                    print(f"\n{TAB_OF_4}```", end='\n\n')
                print()
