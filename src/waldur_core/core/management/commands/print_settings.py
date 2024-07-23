import pprint
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from waldur_core.core.metadata import WaldurConfiguration


def print_section(section, section_name, print_default=False):
    print(f"### {section_name}")
    print()
    if print_default and section.default is not None and section.default != "":
        default_value = section.default
        if isinstance(default_value, timedelta):
            default_value = repr(default_value)
        print(f"Type: {section._type_display()}, default value: {default_value}")
    else:
        print(f"Type: {section._type_display()}")
    if section.field_info.description:
        print()
        print(section.field_info.description)
    print()


def generate_markdown(config, fieldsets):
    markdown = ""

    for group, variables in fieldsets.items():
        markdown += f"## {group}\n\n"
        for variable in variables:
            if variable in config:
                default_value, description, *rest = config[variable]
                var_type = rest[0] if rest else type(default_value).__name__
                markdown += f"### {variable}\n\n"
                markdown += f"**Type:** {var_type}\n\n"
                if default_value:
                    markdown += f"**Default value**: {default_value}\n\n"
                markdown += f"{description}\n\n"

    return markdown


class Command(BaseCommand):
    help = """Prints Waldur configuration options in markdown format."""

    def handle(self, *args, **options):
        sorted_items = sorted(WaldurConfiguration().__fields__.items())
        nested = [
            (section_name, section)
            for (section_name, section) in sorted_items
            if hasattr(section.type_, "__fields__")
        ]
        flat = [
            (section_name, section)
            for (section_name, section) in sorted_items
            if not hasattr(section.type_, "__fields__")
        ]

        print("# Configuration guide for static options", end="\n\n")

        for section_name, section in nested:
            type_ = section.type_
            default_value = pprint.pformat(section.default.dict())
            print(f"## {section_name} plugin")
            print()
            print(
                f"Default value:\n\n```python\n{section_name} = {default_value}\n```\n"
            )
            for field_name, field in sorted(type_.__fields__.items()):
                print_section(field, field_name)

        print("## Other variables")
        print()
        for section_name, section in flat:
            print_section(section, section_name, print_default=True)

        print("# Configuration guide for dynamic options", end="\n\n")
        print(
            generate_markdown(
                settings.CONSTANCE_CONFIG, settings.CONSTANCE_CONFIG_FIELDSETS
            )
        )
