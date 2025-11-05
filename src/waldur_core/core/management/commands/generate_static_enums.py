"""
Management command to generate static enum constants for TypeScript SDK.
"""

import os

from django.core.management.base import BaseCommand

from waldur_core.core.static_enums import generate_typescript_enums


class Command(BaseCommand):
    help = "Generate TypeScript static enum constants file"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default="./sdk-static-enums.ts",
            help="Output file path for TypeScript enums",
        )

    def handle(self, *args, **options):
        output_file = options["output"]

        self.stdout.write(f"Generating static enums to {output_file}...")

        try:
            content = generate_typescript_enums()

            # Ensure output directory exists
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)

            with open(output_file, "w") as f:
                f.write(content)

            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Successfully generated static enums: {output_file}"
                )
            )

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"❌ Failed to generate static enums: {e}")
            )
