from django.core.management.base import BaseCommand

from waldur_core.core.features import FEATURES


class Command(BaseCommand):
    help = """Prints all Waldur feature description as typescript code."""

    def handle(self, *args, **options):
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_features_description.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        print("import { translate } from '@waldur/i18n';")
        print()
        print("import { FeatureSection } from '@waldur/features/types';")
        print()
        print("export const FeaturesDescription: FeatureSection[] = [")
        for section in sorted(FEATURES, key=lambda section: section["key"]):
            print("  {")
            print(f"    key: '{section['key']}',")
            print(f"    description: translate('{section['description']}'),")
            print("    items: [")
            for feature in sorted(section["items"], key=lambda section: section["key"]):
                print("      {")
                print(f"        key: '{feature['key']}',")
                print(f"        description: translate('{feature['description']}'),")
                print("      },")
            print("    ],")
            print("  },")
        print("];")
        print()
