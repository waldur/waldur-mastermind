from django.core.management.base import BaseCommand

from waldur_core.core.features import FEATURES


class Command(BaseCommand):
    help = """Prints all Waldur feature toggles in markdown format."""

    def handle(self, *args, **options):
        print("# Features", end="\n\n")
        features_output = []
        for section in sorted(FEATURES, key=lambda section: section["key"]):
            for feature in sorted(section["items"], key=lambda section: section["key"]):
                feature_block = f"## {section['key']}.{feature['key']}\n\n{feature['description']}\n"
                features_output.append(feature_block)

        # Join all features and remove trailing whitespace to avoid multiple consecutive blank lines
        print("\n".join(features_output).rstrip())
