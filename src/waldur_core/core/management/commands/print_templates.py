import os

from django.conf import settings
from django.core.management.base import BaseCommand

from .print_events import BLANK_LINE


class Command(BaseCommand):
    def handle(self, *args, **options):
        print("# Message templates", end=BLANK_LINE)
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
                print(f"## {app}", end=BLANK_LINE)
                for fname in os.listdir(templates_dir):
                    full_path = os.path.join(templates_dir, fname)
                    if os.path.isfile(full_path) and (
                        full_path.endswith(".html") or full_path.endswith(".txt")
                    ):
                        _, extension = os.path.splitext(fname)
                        print(f"### {fname} ({app})", end=BLANK_LINE)
                        print(f"``` {extension[1:]}")
                        with open(full_path) as template_file:
                            for line in template_file.readlines():
                                print(line, end="")
                        print("```", end=BLANK_LINE)
