import re
from datetime import timedelta

from django.core.management.base import BaseCommand

from waldur_core.server.celery_settings import CELERY_BEAT_SCHEDULE
from waldur_core.server.celeryconf import app


def format_schedule(schedule):
    """Format schedule in a human-readable way."""
    if isinstance(schedule, timedelta):
        total_seconds = int(schedule.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        parts = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if seconds:
            parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

        if not parts:
            return "0 seconds"

        return ", ".join(parts)

    # Handle cron expressions
    schedule_str = str(schedule)
    if "crontab" in schedule_str:
        # Try to parse common cron patterns
        if "0 0 1 * *" in schedule_str:
            return "Monthly (1st day of month at midnight)"
        elif "0 0 * * 0" in schedule_str:
            return "Weekly (every Sunday at midnight)"
        elif "0 0 * * *" in schedule_str:
            return "Daily (at midnight)"
        elif "0 * * * *" in schedule_str:
            return "Hourly (at minute 0)"
        else:
            # Extract cron expression from format like '<crontab: * * 1 * * (m/h/dM/MY/d)>'
            match = re.search(r"<crontab:\s*(.+?)>", schedule_str)
            if match:
                cron_expr = match.group(1)
            else:
                cron_expr = schedule_str

            return f"Cron: {cron_expr}"

    return str(schedule)


def get_task_description(task_name):
    """Get task description from Celery task registry."""
    try:
        # Get task from Celery registry using the same app instance as celeryconf.py
        task = app.tasks.get(task_name)
        if task and hasattr(task, "__doc__") and task.__doc__:
            # Clean up the docstring and make it markdown table-friendly
            doc = task.__doc__.strip()
            # Replace newlines with <br> tags for markdown table cells
            doc = doc.replace("\n", "<br>")
            # Replace multiple spaces with single spaces
            doc = " ".join(doc.split())
            return doc if doc else "No description available"
        elif task:
            return "No description available"
        else:
            return "Task not found in registry"
    except Exception:
        return "Unable to retrieve description"


class Command(BaseCommand):
    help = """Prints all scheduled background jobs in markdown format."""

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-file",
            type=str,
            help="Output file path (optional, defaults to stdout)",
        )

    def handle(self, *args, **options):
        output_file = options.get("output_file")

        # Generate markdown content
        markdown_content = self.generate_markdown()

        # Write to file or stdout
        if output_file:
            with open(output_file, "w") as f:
                f.write(markdown_content)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully written scheduled jobs to {output_file}"
                )
            )
        else:
            self.stdout.write(markdown_content)

    def generate_markdown(self):
        """Generate markdown documentation for all scheduled jobs."""
        markdown = "# Scheduled Background Jobs\n\n"
        markdown += "This document lists all scheduled background jobs (Celery beat tasks) configured in the system.\n\n"

        # Add CSS styling for table column widths
        markdown += """<style>
table {
  table-layout: fixed;
  width: 100%;
}
td:nth-child(1) {
  width: 25%;
  word-wrap: break-word;
}
td:nth-child(2) {
  width: 25%;
  word-wrap: break-word;
}
td:nth-child(3) {
  width: 15%;
  word-wrap: break-word;
}
td:nth-child(4) {
  width: 35%;
  word-wrap: break-word;
}
</style>

"""

        all_jobs = {}

        # all jobs are loaded into CELERY_BEAT_SCHEDULE
        for name, config in CELERY_BEAT_SCHEDULE.items():
            all_jobs[name] = config

        # Write jobs section
        markdown += "## Scheduled Jobs\n\n"

        if all_jobs:
            markdown += "| Job Name | Task | Schedule | Description |\n"
            markdown += "|----------|------|----------|-------------|\n"

            for job_name, job_config in sorted(all_jobs.items()):
                task_name = job_config.get("task", "N/A")
                schedule = format_schedule(job_config.get("schedule", "N/A"))
                description = get_task_description(task_name)
                markdown += (
                    f"| `{job_name}` | `{task_name}` | {schedule} | {description} |\n"
                )
        else:
            markdown += "No jobs found.\n"

        markdown += "\n"

        return markdown
