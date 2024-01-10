from django.core.management.base import BaseCommand

from waldur_core.logging.loggers import event_logger

CHAR_LINE_LIMIT = 80


class Command(BaseCommand):
    help = """Prints all event types as typescript enums in prettify format."""

    def handle(self, *args, **options):
        groups = sorted([(k, v) for k, v in event_logger.get_all_groups().items()])
        print(
            """/* eslint-disable */
// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_events_enums.py
// Do not edit it manually. All manual changes would be overridden.""",
            end="\n",
        )
        for event_group, events in groups:
            print(f"\nexport const {str(event_group).capitalize()}Enum = {{")
            for event in sorted(events):
                line = f"  {event}: '{event}',"
                if len(line) >= CHAR_LINE_LIMIT:
                    line = f"  {event}:\n    '{event}',"
                print(line)
            print("};")
