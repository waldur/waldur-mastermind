from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from waldur_mastermind.marketplace.models import ComponentUsagePollRecord


class Command(BaseCommand):
    help = "Delete ComponentUsagePollRecord entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-months",
            type=int,
            default=0,
            help="Only delete records older than N months (0 = delete all).",
        )

    def handle(self, *args, **options):
        months = options["older_than_months"]
        qs = ComponentUsagePollRecord.objects.all()
        if months:
            cutoff = timezone.now() - timedelta(days=months * 30)
            qs = qs.filter(last_poll_time__lt=cutoff)
        count = qs.count()
        qs.delete()
        self.stdout.write(f"Deleted {count} usage poll records.")
