from django.core.management.base import BaseCommand

from waldur_mastermind.policy import tasks


class Command(BaseCommand):
    help = (
        "Manually trigger cleanup of old SLURM policy evaluation logs. "
        "Uses the SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS constance setting."
    )

    def handle(self, *args, **options):
        self.stdout.write("Running SLURM evaluation log cleanup...")
        result = tasks.cleanup_slurm_evaluation_logs()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {result['deleted_count']} log entries "
                f"older than {result['retention_days']} days."
            )
        )
