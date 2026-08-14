from django.conf import settings
from django.core.management.base import BaseCommand

from waldur_core.core.fields import COUNTRIES


class Command(BaseCommand):
    help = """Prints all Waldur feature description as typescript code."""

    def handle(self, *args, **options):
        print(
            "// WARNING: This file is auto-generated from src/waldur_core/core/management/commands/print_settings_description.py"
        )
        print("// Do not edit it manually. All manual changes would be overridden.")
        print("import { translate } from '@/i18n';")
        print()
        print("export const SettingsDescription = [")
        for title, keys in settings.CONSTANCE_CONFIG_FIELDSETS.items():
            print("  {")
            print(f"    description: translate('{title}'),")
            print("    items: [")
            for key in keys:
                default = settings.CONSTANCE_CONFIG[key][0]
                description = settings.CONSTANCE_CONFIG[key][1].replace("'", "\\'")
                value_type = None
                config_type = None
                if len(settings.CONSTANCE_CONFIG[key]) >= 3:
                    raw_type = settings.CONSTANCE_CONFIG[key][2]
                    if isinstance(raw_type, type):
                        type_map = {
                            int: "integer",
                            float: "float",
                            bool: "boolean",
                            str: "string",
                            list: "list_field",
                        }
                        config_type = type_map.get(raw_type, raw_type.__name__)
                    else:
                        config_type = raw_type
                    value_type = f"'{config_type}'"
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
                choices = None
                if (
                    hasattr(settings, "CONSTANCE_CONFIG_CHOICES")
                    and key in settings.CONSTANCE_CONFIG_CHOICES
                ):
                    choices = settings.CONSTANCE_CONFIG_CHOICES[key]
                elif config_type == "country_list_field":
                    # The default is only the shipped subset; expose every valid
                    # country so the frontend can offer all of them.
                    choices = COUNTRIES

                options_metadata = ""
                if choices:
                    formatted_choices = ", ".join(
                        [
                            "{{ value: '{}', label: '{}' }}".format(
                                str(c[0]).replace("'", "\\'"),
                                str(c[1]).replace("'", "\\'"),
                            )
                            for c in choices
                        ]
                    )
                    options_metadata = f"        options: [{formatted_choices}],\n"

                print("      {")
                print(f"        key: '{key}',")
                print(f"        description: translate('{description}'),")
                print(f"        default: {formatted_default},")
                print(f"        type: {formatted_type},")
                if options_metadata:
                    print(options_metadata, end="")
                print("      },")
            print("    ],")
            print("  },")
        print("];")
        print()
