from django.core.management.base import BaseCommand

from waldur_mastermind.support.utils import get_helpdesk_stats


class Command(BaseCommand):
    help = "Display helpdesk statistics summary."

    def handle(self, *args, **options):
        stats = get_helpdesk_stats()

        self.stdout.write(self.style.MIGRATE_HEADING("Helpdesk Statistics"))
        self.stdout.write(f"  Open issues:            {stats['total_open']}")
        self.stdout.write(
            f"  Closed this month:      {stats['total_closed_this_month']}"
        )
        self.stdout.write(f"  Routed to providers:    {stats['total_routed']}")
        self.stdout.write(f"  Escalated:              {stats['total_escalated']}")
        self.stdout.write(f"  SLA breaches:           {stats['sla_breach_count']}")

        avg_resp = stats.get("avg_first_response_hours")
        avg_res = stats.get("avg_resolution_hours")
        self.stdout.write(
            f"  Avg first response:     {f'{avg_resp:.1f}h' if avg_resp else 'N/A'}"
        )
        self.stdout.write(
            f"  Avg resolution time:    {f'{avg_res:.1f}h' if avg_res else 'N/A'}"
        )

        self.stdout.write("\n  By Status:")
        for s, count in stats.get("by_status", {}).items():
            self.stdout.write(f"    {s}: {count}")

        self.stdout.write("\n  By Priority:")
        for p, count in stats.get("by_priority", {}).items():
            self.stdout.write(f"    {p}: {count}")
