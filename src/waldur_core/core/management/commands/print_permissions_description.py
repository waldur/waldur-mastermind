from django.core.management.base import BaseCommand

from waldur_core.permissions.enums import PERMISSION_DESCRIPTION


class Command(BaseCommand):
    help = """Prints all Waldur permissions description as typescript code."""

    def handle(self, *args, **options):
        print("/* eslint-disable prettier/prettier */")
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_permissions_description.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        print("import { translate } from '@waldur/i18n';")
        print()
        print("export const PermissionOptions = [")
        for section in PERMISSION_DESCRIPTION:
            print("  {")
            print(f"    label: translate('{section['label']}'),")
            print("    options: [")
            for option in section["options"]:
                print("      {")
                print(f"        label: translate('{option['label']}'),")
                print(f"        value: '{option['value']}',")
                print("      },")
            print("    ],")
            print("  },")
        print("];")
