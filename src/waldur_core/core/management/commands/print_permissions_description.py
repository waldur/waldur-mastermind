from django.core.management.base import BaseCommand

from waldur_core.permissions.enums import PERMISSION_DESCRIPTION


def ts_string(value) -> str:
    """``value`` as a single-quoted TypeScript string literal.

    A label such as "the offering's agent" would otherwise end the string at
    the apostrophe and break the generated file.
    """
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"'{escaped}'"


class Command(BaseCommand):
    help = """Prints all Waldur permissions description as typescript code."""

    def handle(self, *args, **options):
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_permissions_description.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        print("import { translate } from '@/i18n';")
        print()
        print("export const PermissionOptions = [")
        for section in PERMISSION_DESCRIPTION:
            print("  {")
            print(f"    label: translate({ts_string(section['label'])}),")
            print("    options: [")
            for option in section["options"]:
                print("      {")
                print(f"        label: translate({ts_string(option['label'])}),")
                print(f"        value: {ts_string(option['value'])},")
                print("      },")
            print("    ],")
            print("  },")
        print("];")
