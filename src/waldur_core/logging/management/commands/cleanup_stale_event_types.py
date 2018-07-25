from django.core.management.base import BaseCommand

from waldur_core.logging import loggers
from waldur_core.logging.models import EventTypesMixin


class Command(BaseCommand):
    help = "Cleanup stale event types in all hooks."

    def handle(self, *args, **options):
        self.stdout.write('Checking event types of hooks...')

        valid_events = loggers.get_valid_events()
        changed_hooks = 0
        for model in EventTypesMixin.get_all_models():
            for hook in model.objects.all():
                clean_events = filter(lambda x: x in valid_events, hook.event_types)
                if clean_events != hook.event_types:
                    hook.event_types = clean_events
                    hook.save(update_fields=['event_types'])
                    changed_hooks += 1

        if changed_hooks == 0:
            self.stdout.write('All hooks have valid event types.')
        elif changed_hooks == 1:
            self.stdout.write('1 hook has been updated.')
        else:
            self.stdout.write('%s hooks have been updated.' % changed_hooks)
