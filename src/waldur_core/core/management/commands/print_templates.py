import os

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = """Prints all Waldur templates in markdown format."""

    def handle(self, *args, **options):
        output = ["# Message templates\n"]

        possible_dirs = [
            (
                os.path.join(
                    settings.BASE_DIR,
                    "src",
                    app.replace(".", "/"),
                    "templates",
                    app.split(".")[1] if len(app.split(".")) == 2 else app,
                ),
                app,
            )
            for app in settings.INSTALLED_APPS
            if "waldur" in app and "landing" not in app
        ]

        for templates_dir, app in possible_dirs:
            if os.path.isdir(templates_dir):
                output.append(f"## {app}\n")
                for fname in os.listdir(templates_dir):
                    full_path = os.path.join(templates_dir, fname)
                    if os.path.isfile(full_path) and (
                        full_path.endswith(".html") or full_path.endswith(".txt")
                    ):
                        _, extension = os.path.splitext(fname)
                        output.append(f"### {fname} ({app})\n")
                        output.append(f"```{extension[1:]}\n")
                        with open(full_path) as template_file:
                            content = template_file.read()
                            output.append(content)
                        output.append("```\n")

        # Join all output and remove trailing whitespace to avoid multiple consecutive blank lines
        result = "\n".join(output).rstrip()
        print(result)
