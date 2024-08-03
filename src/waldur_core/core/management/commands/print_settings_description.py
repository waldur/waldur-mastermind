from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = """Prints all Waldur feature description as typescript code."""

    def handle(self, *args, **options):
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_settings_description.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        print("import { translate } from '@waldur/i18n';")
        print()
        print("export const SettingsDescription = [")
        for title, keys in settings.CONSTANCE_CONFIG_FIELDSETS.items():
            print("  {")
            print(f"    description: translate('{title}'),")
            print("    items: [")
            for key in keys:
                default = settings.CONSTANCE_CONFIG[key][0]
                description = settings.CONSTANCE_CONFIG[key][1].replace("'", "\\'")
                value_type = (
                    len(settings.CONSTANCE_CONFIG[key]) == 3
                    and f"'{settings.CONSTANCE_CONFIG[key][2]}'"
                    or None
                )
                formatted_default = (
                    isinstance(default, str)
                    and f"'{default}'"
                    or default is True
                    and "true"
                    or default is False
                    and "false"
                    or default
                )
                formatted_type = (
                    value_type
                    or isinstance(default, str)
                    and "'string'"
                    or isinstance(default, bool)
                    and "'boolean'"
                    or isinstance(default, int)
                    and "'integer'"
                )
                print("      {")
                print(f"        key: '{key}',")
                print(f"        description: translate('{description}'),")
                print(f"        default: {formatted_default},")
                print(f"        type: {formatted_type},")
                print("      },")
            print("    ],")
            print("  },")
        print("];")
        print()
