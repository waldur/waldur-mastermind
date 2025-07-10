import importlib
import inspect
import pkgutil
import re

from django.apps import apps
from django.core.management.base import BaseCommand


def get_waldur_modules():
    """Get all Waldur modules from installed apps and known core modules."""
    modules = set()

    # Core waldur modules that should always be checked
    core_modules = [
        "waldur_core.core",
        "waldur_core.structure",
        "waldur_core.permissions",
        "waldur_core.logging",
        "waldur_core.media",
        "waldur_core.quotas",
        "waldur_core.users",
    ]

    # Add core modules
    modules.update(core_modules)

    # Get modules from Django apps
    for app_config in apps.get_app_configs():
        app_name = app_config.name
        if app_name.startswith("waldur_"):
            modules.add(app_name)

    return sorted(modules)


def find_mixins_in_module(module_name):
    """Find all mixin classes in a specific module and its submodules."""
    mixins = []

    try:
        # Import the main module
        main_module = importlib.import_module(module_name)

        # Get all submodules
        if hasattr(main_module, "__path__"):
            for importer, modname, ispkg in pkgutil.walk_packages(
                main_module.__path__, main_module.__name__ + "."
            ):
                try:
                    submodule = importlib.import_module(modname)
                    mixins.extend(extract_mixins_from_module(submodule, modname))
                except (ImportError, AttributeError, TypeError):
                    continue
        else:
            # Single module, not a package
            mixins.extend(extract_mixins_from_module(main_module, module_name))

    except (ImportError, AttributeError, TypeError):
        pass

    return mixins


def extract_mixins_from_module(module, module_name):
    """Extract mixin classes from a loaded module."""
    mixins = []

    try:
        # Get all classes in the module
        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Check if class name ends with "Mixin" and is defined in this module
            if (
                name.endswith("Mixin")
                and hasattr(obj, "__module__")
                and obj.__module__ == module_name
            ):
                # Get docstring
                docstring = inspect.getdoc(obj)
                if not docstring:
                    docstring = "No description available"

                # Get base classes
                bases = [
                    base.__name__ for base in obj.__bases__ if base.__name__ != "object"
                ]

                mixin_info = {
                    "name": name,
                    "module": module_name,
                    "description": docstring.strip()
                    if docstring
                    else "No description available",
                    "bases": bases,
                }
                mixins.append(mixin_info)

    except Exception:
        pass

    return mixins


def find_all_mixins():
    """Find all mixin classes in the Waldur codebase."""
    mixins = []

    # Get all Waldur modules
    waldur_modules = get_waldur_modules()

    # Find mixins in each module
    for module_name in waldur_modules:
        module_mixins = find_mixins_in_module(module_name)
        mixins.extend(module_mixins)

    # Sort mixins by module and name
    mixins.sort(key=lambda x: (x["module"], x["name"]))
    return mixins


class Command(BaseCommand):
    help = """Prints all mixin classes in the codebase in markdown format."""

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-file",
            type=str,
            help="Output file path (optional, defaults to stdout)",
        )

    def handle(self, *args, **options):
        output_file = options.get("output_file")

        # Generate markdown content
        markdown_content = self.generate_markdown()

        # Write to file or stdout
        if output_file:
            with open(output_file, "w") as f:
                f.write(markdown_content)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully written mixins documentation to {output_file}"
                )
            )
        else:
            self.stdout.write(markdown_content)

    def generate_markdown(self):
        """Generate markdown documentation for all mixin classes."""
        markdown = "# Mixin classes documentation\n\n"
        markdown += (
            "This document lists all mixin classes found in the Waldur codebase.\n\n"
        )

        all_mixins = find_all_mixins()

        if not all_mixins:
            markdown += "No mixin classes found.\n"
            return markdown

        # Write summary table
        markdown += "| Mixin Name | Module | Short Description |\n"
        markdown += "|------------|--------|-------------------|\n"

        for mixin in all_mixins:
            name = mixin["name"]
            module = mixin["module"]
            description = mixin["description"]

            # Create short description (first sentence or up to 80 chars)
            short_desc = (
                description.split(".")[0] if "." in description else description
            )
            if len(short_desc) > 80:
                short_desc = short_desc[:77] + "..."

            # Clean up for table cell
            short_desc = short_desc.replace("\n", " ").replace("|", "\\|").strip()

            # Create anchor link
            anchor = name.lower().replace("mixin", "mixin")

            markdown += f"| [`{name}`](#{anchor}) | `{module}` | {short_desc} |\n"

        # Write detailed descriptions
        markdown += "\n## Detailed Descriptions\n\n"

        for mixin in all_mixins:
            name = mixin["name"]
            module = mixin["module"]
            description = mixin["description"]

            # Create anchor
            anchor = name.lower().replace("mixin", "mixin")

            markdown += f"### {name}\n\n"
            markdown += f"**Module:** `{module}`\n\n"

            # Format description with proper markdown
            if description and description != "No description available":
                # Clean and format description
                formatted_desc = description.replace("<br>", "\n").strip()

                # Fix list formatting
                formatted_desc = re.sub(
                    r"^(\d+\.)  ", r"\1 ", formatted_desc, flags=re.MULTILINE
                )
                formatted_desc = re.sub(
                    r"^\* +", "- ", formatted_desc, flags=re.MULTILINE
                )
                formatted_desc = re.sub(
                    r"^  - ", "- ", formatted_desc, flags=re.MULTILINE
                )

                markdown += f"**Description:**\n{formatted_desc}\n\n"
            else:
                markdown += "**Description:** No description available.\n\n"

            # Add base classes if available
            if mixin.get("bases"):
                bases_str = ", ".join(f"`{base}`" for base in mixin["bases"])
                markdown += f"**Base classes:** {bases_str}\n\n"

        return markdown
