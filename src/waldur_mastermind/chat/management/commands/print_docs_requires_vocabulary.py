import json

from django.core.management.base import BaseCommand

from waldur_mastermind.chat import deployment_profile


class Command(BaseCommand):
    help = (
        "Print, as JSON, every key a documentation section's `requires` term "
        "may name: settings, feature flags, offering types and capabilities. "
        "Docs CI validates the corpus against it."
    )

    def handle(self, *args, **options):
        self.stdout.write(
            json.dumps(deployment_profile.vocabulary(), indent=2, sort_keys=True)
        )
