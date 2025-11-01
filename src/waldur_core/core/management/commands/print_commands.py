from argparse import ArgumentParser

from django.core.management import get_commands, load_command_class
from django.core.management.base import BaseCommand

BLACK_LIST = [
    "print_commands",
    "print_settings",
    "print_features",
    "print_schema",
    "print_events",
    "print_templates",
]

WHITE_LIST = [
    "waldur",
    "axes",
]


class Command(BaseCommand):
    help = """Prints description all Waldur management commands in markdown format."""

    def handle(self, *args, **options):
        commands = []
        for name, path in get_commands().items():
            if not any(map(lambda x: x in path, WHITE_LIST)) or name in BLACK_LIST:
                continue
            command = load_command_class(path, name)
            commands.append((name, command))

        output = ["# CLI guide\n"]

        for name, command in sorted(commands, key=lambda x: x[0]):
            parser = ArgumentParser(prog=f"waldur {name}", add_help=False)
            command.add_arguments(parser)

            # Build command section
            output.append(f"## {name}\n")

            # Clean up help text and ensure consistent formatting
            help_text = command.help.strip().replace("  ", " ")

            # Convert indented code blocks to fenced code blocks for consistency,
            # but skip lines that are already inside fenced code blocks
            import re

            lines = help_text.split("\n")
            processed_lines = []
            in_fenced_block = False
            in_indented_code_block = False

            for i, line in enumerate(lines):
                # Check if we're entering or leaving a fenced code block
                if re.match(r"^\s*```", line):
                    in_fenced_block = not in_fenced_block
                    processed_lines.append(line)
                    continue

                # If we're inside a fenced code block, don't process indentation
                if in_fenced_block:
                    processed_lines.append(line)
                    continue

                # Detect indented code blocks (4+ spaces or tab at start, followed by content)
                if re.match(r"^(\s{4,}|\t+).+", line) and not in_indented_code_block:
                    # Add blank line before code block if previous line isn't empty
                    if processed_lines and processed_lines[-1].strip():
                        processed_lines.append("")
                    processed_lines.append("```yaml")
                    processed_lines.append(line.lstrip())
                    in_indented_code_block = True
                elif in_indented_code_block and re.match(r"^(\s{4,}|\t+).+", line):
                    processed_lines.append(line.lstrip())
                elif in_indented_code_block and (
                    not re.match(r"^(\s{4,}|\t+)", line) or i == len(lines) - 1
                ):
                    processed_lines.append("```")
                    in_indented_code_block = False
                    # Add blank line after code block if current line isn't empty
                    if line.strip():
                        processed_lines.append("")
                        processed_lines.append(line)
                    elif not line.strip():
                        processed_lines.append(line)
                else:
                    processed_lines.append(line)

            # Close any open indented code block
            if in_indented_code_block:
                processed_lines.append("```")

            help_text = "\n".join(processed_lines)
            output.append(f"{help_text}\n")

            if parser._actions:
                output.append("```bash\n")
                # Capture help output to a string instead of printing directly
                import io
                import sys

                old_stdout = sys.stdout
                sys.stdout = help_buffer = io.StringIO()
                try:
                    parser.print_help()
                    help_output = help_buffer.getvalue()
                finally:
                    sys.stdout = old_stdout

                output.append(f"{help_output}")
                output.append("```\n")

        # Join all output and remove trailing whitespace to avoid multiple consecutive blank lines
        result = "\n".join(output).rstrip()
        print(result)
