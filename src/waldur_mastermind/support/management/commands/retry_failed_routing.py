from django.core.management.base import BaseCommand

from waldur_mastermind.support import models, tasks


class Command(BaseCommand):
    help = "Retry routing for issues that failed to be routed to a provider."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only list issues that would be retried, without actually retrying.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Find issues that should have been routed but have no child issues
        unrouted_issues = models.Issue.objects.filter(
            parent_issue__isnull=True,
            child_issues__isnull=True,
            provider_helpdesk__isnull=False,
        ).distinct()

        if not unrouted_issues.exists():
            self.stdout.write("No issues need routing retry.")
            return

        self.stdout.write(f"Found {unrouted_issues.count()} issue(s) to retry.")

        for issue in unrouted_issues:
            self.stdout.write(f"  {issue.key}: {issue.summary}")
            if not dry_run:
                tasks.route_issue_to_provider.delay(issue.id)
                self.stdout.write(self.style.SUCCESS("    -> Routing task dispatched"))

        if dry_run:
            self.stdout.write("\nDry run complete. No tasks dispatched.")
