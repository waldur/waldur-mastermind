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

                # Fix list formatting with proper blank lines around lists
                formatted_desc = self._format_lists_with_blank_lines(formatted_desc)

                # Fix code blocks to use consistent fenced style
                formatted_desc = self._format_code_blocks(formatted_desc)

                # Fix any header indentation issues
                formatted_desc = self._fix_header_indentation(formatted_desc)

                markdown += f"**Description:**\n\n{formatted_desc}\n\n"
            else:
                markdown += "**Description:** No description available.\n\n"

            # Add base classes if available
            if mixin.get("bases"):
                bases_str = ", ".join(f"`{base}`" for base in mixin["bases"])
                markdown += f"**Base classes:** {bases_str}\n\n"

        # Remove any trailing extra blank lines and ensure single trailing newline
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        markdown = markdown.rstrip() + "\n"

        return markdown

    def _format_lists_with_blank_lines(self, text):
        """Format lists to ensure proper blank lines around them."""
        lines = text.split("\n")
        formatted_lines = []
        i = 0

        while i < len(lines):
            line = lines[i]

            # Check if current line is a list item
            is_list_item = re.match(r"^\s*[-*+]\s+", line) or re.match(
                r"^\s*\d+\.\s+", line
            )

            if is_list_item:
                # Add blank line before list if previous line exists and isn't blank
                if formatted_lines and formatted_lines[-1].strip():
                    formatted_lines.append("")

                # Track list numbering for ordered lists
                list_number = 1

                # Add all consecutive list items (including ones separated by blank lines and continuation text)
                while i < len(lines):
                    current_line = lines[i]

                    # Check if this is a list item
                    if re.match(r"^\s*[-*+]\s+", current_line):
                        # Convert to dash and normalize spacing
                        lines[i] = re.sub(r"^\s*[-*+]\s*", "- ", current_line)
                        formatted_lines.append(lines[i])
                        i += 1
                    elif re.match(r"^\s*\d+\.\s+", current_line):
                        # Normalize numbered list with sequential numbering
                        content = re.sub(r"^\s*\d+\.\s*", "", current_line)
                        lines[i] = f"{list_number}. {content}"
                        list_number += 1
                        formatted_lines.append(lines[i])
                        i += 1
                    elif current_line.strip() == "":
                        # Empty line - check what comes next
                        formatted_lines.append(current_line)
                        i += 1

                        # If the line after the empty line is a list item, continue
                        if i < len(lines) and (
                            re.match(r"^\s*[-*+]\s+", lines[i])
                            or re.match(r"^\s*\d+\.\s+", lines[i])
                        ):
                            continue
                        # If the line after empty line is indented and followed by a list item, it's continuation
                        elif (
                            i < len(lines)
                            and re.match(r"^\s+\S", lines[i])
                            and i + 1 < len(lines)
                            and lines[i + 1].strip() == ""
                            and i + 2 < len(lines)
                            and (
                                re.match(r"^\s*[-*+]\s+", lines[i + 2])
                                or re.match(r"^\s*\d+\.\s+", lines[i + 2])
                            )
                        ):
                            continue
                        else:
                            # End of list
                            i -= 1  # Back up to process this line outside the list
                            break
                    elif re.match(r"^\s+\S", current_line):
                        # Indented line - could be continuation text
                        # Check if there's a list item coming up after this block
                        j = i + 1
                        while j < len(lines) and (
                            lines[j].strip() == "" or re.match(r"^\s+\S", lines[j])
                        ):
                            j += 1

                        if j < len(lines) and (
                            re.match(r"^\s*[-*+]\s+", lines[j])
                            or re.match(r"^\s*\d+\.\s+", lines[j])
                        ):
                            # This is continuation text for the list
                            formatted_lines.append(current_line)
                            i += 1
                        else:
                            # Not part of the list anymore
                            break
                    else:
                        # Not part of the list anymore
                        break

                # Add blank line after list if next line exists and isn't blank
                if i < len(lines) and lines[i].strip():
                    formatted_lines.append("")
            else:
                formatted_lines.append(line)
                i += 1

        return "\n".join(formatted_lines)

    def _format_code_blocks(self, text):
        """Format code blocks to use consistent fenced style."""
        # Convert indented code blocks to fenced code blocks
        lines = text.split("\n")
        formatted_lines = []
        in_code_block = False
        code_lines = []

        i = 0
        while i < len(lines):
            line = lines[i]

            # Check if line is indented code (4+ spaces or 1+ tabs at start)
            if re.match(r"^(\s{4,}|\t+)", line) and not in_code_block:
                # Add blank line before code block if needed
                if formatted_lines and formatted_lines[-1].strip():
                    formatted_lines.append("")

                # Start collecting code block
                in_code_block = True
                code_lines = []

                # Collect all indented lines
                while i < len(lines) and (
                    lines[i].strip() == "" or re.match(r"^(\s{4,}|\t+)", lines[i])
                ):
                    if lines[i].strip():
                        # Remove indentation (4 spaces or 1 tab)
                        code_lines.append(re.sub(r"^(\s{4}|\t)", "", lines[i]))
                    else:
                        code_lines.append("")
                    i += 1

                # Add fenced code block with python language
                formatted_lines.append("```python")
                formatted_lines.extend(code_lines)
                formatted_lines.append("```")

                # Add blank line after code block if needed
                if i < len(lines) and lines[i].strip():
                    formatted_lines.append("")

                in_code_block = False
                code_lines = []
                continue
            else:
                formatted_lines.append(line)
                i += 1

        # Fix existing code blocks to have blank lines around them
        final_lines = []
        i = 0
        while i < len(formatted_lines):
            line = formatted_lines[i]

            if line.startswith("```"):
                # Add blank line before code block if needed
                if final_lines and final_lines[-1].strip():
                    final_lines.append("")

                # Add the code block
                final_lines.append(line)
                i += 1

                # Add all code content until closing ```
                while i < len(formatted_lines) and not formatted_lines[i].startswith(
                    "```"
                ):
                    final_lines.append(formatted_lines[i])
                    i += 1

                # Add closing ```
                if i < len(formatted_lines):
                    final_lines.append(formatted_lines[i])
                    i += 1

                # Add blank line after code block if needed
                if i < len(formatted_lines) and formatted_lines[i].strip():
                    final_lines.append("")
            else:
                final_lines.append(line)
                i += 1

        return "\n".join(final_lines)

    def _fix_header_indentation(self, text):
        """Fix any header indentation issues."""
        lines = text.split("\n")
        fixed_lines = []

        for line in lines:
            # If line starts with whitespace followed by # (markdown header), remove the whitespace
            if re.match(r"^\s+#", line):
                line = re.sub(r"^\s+", "", line)
            fixed_lines.append(line)

        return "\n".join(fixed_lines)
