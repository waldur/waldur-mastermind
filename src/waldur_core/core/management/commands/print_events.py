from django.core.management.base import BaseCommand

from waldur_core.logging.event_logger import get_event_groups

BLANK_LINE = "\n\n"


class Command(BaseCommand):
    help = """Prints all Waldur events in markdown format."""

    def handle(self, *args, **options):
        print("# Events", end=BLANK_LINE)
        groups = sorted([(k, v) for k, v in get_event_groups().items()])
        for event_group, events in groups:
            print(f"## {str(event_group).capitalize()}", end=BLANK_LINE)
            for event in sorted(events):
                print(f"- {event}")
            print()
