from django.core.management.base import BaseCommand

from waldur_core.logging.loggers import event_logger


class Command(BaseCommand):
    help = """Prints all event types as typescript enums."""

    def handle(self, *args, **options):
        groups = sorted([(k, v) for k, v in event_logger.get_all_groups().items()])
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_events_enums.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        for event_group, events in groups:
            print(f"\nexport const {str(event_group).capitalize()}Enum = {{")
            for event in sorted(events):
                print(f"  {event}: '{event}',")
            print("};")
