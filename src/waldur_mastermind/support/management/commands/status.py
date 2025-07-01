import logging

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.utils import OperationalError

from waldur_core.server.celeryconf import app as celery_app


class Command(BaseCommand):
    help = "Check status of Waldur MasterMind configured services"

    def handle(self, *args, **options):
        success_status = self.style.SUCCESS(" [OK]")
        error_status = self.style.ERROR(" [ERROR]")
        output_messages = {
            "database": " - Database %(vendor)s connection",
            "workers": " - Task runners (Celery workers)",
        }
        padding = len(max(output_messages.values(), key=len))
        # If services checks didn't pass, skip API endpoints check
        erred = False

        # Rise logging level to prevent redundant log messages
        logging.disable(logging.CRITICAL)
        self.stdout.write("Checking Waldur MasterMind services...")

        # Check database connectivity
        db_vendor = (
            connection.vendor.capitalize().replace("sql", "SQL").replace("Sql", "SQL")
        )
        self.stdout.write(
            (output_messages["database"] % {"vendor": db_vendor}).ljust(padding),
            ending="",
        )
        try:
            connection.cursor()
        except OperationalError:
            erred = True
            self.stdout.write(error_status)
        else:
            self.stdout.write(success_status)

        # Check celery
        celery_inspect = celery_app.control.inspect()
        celery_results = {
            "workers": success_status,
        }
        try:
            stats = celery_inspect.stats()
            if not stats:
                erred = True
                celery_results["workers"] = error_status
        except Exception:
            erred = True
            celery_results["workers"] = error_status
        finally:
            self.stdout.write(
                output_messages["workers"].ljust(padding) + celery_results["workers"]
            )

        if erred:
            exit(1)

        # return logging level back
        logging.disable(logging.NOTSET)
