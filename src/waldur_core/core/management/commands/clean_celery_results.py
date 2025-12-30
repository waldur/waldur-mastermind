"""Management command to clean up old Celery task results from database."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Clean up old Celery task results from the database to prevent bloat."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Delete results older than this many hours (default: 24)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many results would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        hours = options["hours"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(hours=hours)

        with connection.cursor() as cursor:
            # Check if celery_taskmeta table exists
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'celery_taskmeta'
                )
                """
            )
            table_exists = cursor.fetchone()[0]

            if not table_exists:
                self.stdout.write(
                    self.style.WARNING(
                        "celery_taskmeta table does not exist, nothing to clean up"
                    )
                )
                return

            if dry_run:
                cursor.execute(
                    "SELECT COUNT(*) FROM celery_taskmeta WHERE date_done < %s",
                    [cutoff],
                )
                count = cursor.fetchone()[0]
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Would delete {count} task results older than {hours} hours"
                    )
                )
            else:
                cursor.execute(
                    "DELETE FROM celery_taskmeta WHERE date_done < %s",
                    [cutoff],
                )
                deleted_count = cursor.rowcount
                if deleted_count > 0:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Deleted {deleted_count} task results older than {hours} hours"
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS("No old task results to clean up")
                    )
