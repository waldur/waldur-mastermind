import re

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import engines

from waldur_core.structure.notifications import NOTIFICATIONS

TAB_OF_4 = " " * 4

CUSTOM_LOADER_SETTING = (
    "admin_tools.template_loaders.Loader",
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
)


class Command(BaseCommand):
    help = """Prints Mastermind notifications with a description and templates"""

    def handle(self, *args, **options):
        file_engine = engines.all()
        # reset loaders to use only filesystem based
        file_engine[0].engine.loaders = CUSTOM_LOADER_SETTING
        # reset cached_property
        del file_engine[0].engine.__dict__["template_loaders"]

        print("# Notifications", end="\n\n")
        for key, section in NOTIFICATIONS.items():
            for app in settings.INSTALLED_APPS:
                plugin = app.split(".")[1] if len(app.split(".")) == 2 else app
                if key == plugin or f"waldur_{key}" == plugin:
                    print(f"## {app.upper()}", end="\n\n")
            for notification in sorted(
                section, key=lambda notification: notification["path"]
            ):
                print(f'### {key}.{notification["path"]}', end="\n\n")
                print(notification["description"], end="\n\n")
                print("#### Templates", end="\n\n")
                for template in notification["templates"]:
                    template_path = f"{key}/{template.path}"
                    print(f'=== "{template_path}"', end="\n\n")
                    print("```txt")
                    source = file_engine[0].get_template(template_path).template.source
                    source = source.replace("\n", f"\n{TAB_OF_4}")
                    source = re.sub(" +\n", "\n", source)
                    source = source.rstrip()
                    print(f"{TAB_OF_4}{source}", end="\n")
                    print("\n```", end="\n\n")
