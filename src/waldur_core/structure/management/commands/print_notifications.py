import re

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import engines

from waldur_core.structure.notifications import NOTIFICATIONS

TAB_OF_4 = " " * 4

CUSTOM_LOADER_SETTING = (
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
        file_engine[0].engine.__dict__.pop("template_loaders", None)

        output = [
            "# Notifications\n",
            "When a notification is removed from a release, its database row is "
            "not deleted automatically. Run `waldur load_notifications <file> "
            "--prune` to report and remove notifications whose key is no longer "
            "listed below, along with any of their templates that no other "
            "notification declares and that have no operator-customised "
            "content. Customised template content is never deleted "
            "automatically.\n",
        ]

        for key, section in NOTIFICATIONS.items():
            for app in settings.INSTALLED_APPS:
                plugin = app.split(".")[1] if len(app.split(".")) == 2 else app
                if key == plugin or f"waldur_{key}" == plugin:
                    output.append(f"## {app.upper()}\n")
            for notification in sorted(
                section, key=lambda notification: notification["path"]
            ):
                output.append(f"### {key}.{notification['path']}\n")
                output.append(f"{notification['description']}\n")
                output.append("#### Templates\n")
                for template in notification["templates"]:
                    template_path = f"{key}/{template['path']}"
                    output.append(f'=== "{template_path}"\n')
                    output.append("```txt\n")
                    source = file_engine[0].get_template(template_path).template.source
                    source = source.replace("\n", f"\n{TAB_OF_4}")
                    source = re.sub(" +\n", "\n", source)
                    source = source.rstrip()
                    output.append(f"{TAB_OF_4}{source}\n")
                    output.append("```\n")

        # Join all output with proper spacing and remove trailing whitespace
        result = "\n".join(output).rstrip()
        print(result)
