from django.core.management.base import BaseCommand

from waldur_core.core.features import FEATURES


class Command(BaseCommand):
    def handle(self, *args, **options):
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_features_enums.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        sections = []
        for section in sorted(FEATURES, key=lambda section: section["key"]):
            section_key = section["key"]
            title = section_key.capitalize() + "Features"
            sections.append(title)
            print(f"\nexport enum {title} {{")

            for feature in sorted(section["items"], key=lambda section: section["key"]):
                print(f'  {feature["key"]} = \'{section_key}.{feature["key"]}\',')
            print("}")
        print()
        print("export type FeaturesEnum =")
        for section in sections:
            print(f"  | {section}")
        print(";")
