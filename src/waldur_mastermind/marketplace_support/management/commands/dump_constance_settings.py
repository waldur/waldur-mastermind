import os
import shutil

import yaml
from constance import LazyConfig, settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from waldur_core.core import logos

WHITELABELING_LOGOS = logos.LOGO_MAP.keys()


class Command(BaseCommand):
    help = """
    Dump all settings stored in django-constance to a YAML file.
    This includes all settings, even those with file/image values.

    Usage:
        waldur dump_constance_settings output.yaml

    The output format is compatible with override_constance_settings command.

    For image/file fields, you can optionally export the actual files to a directory
    using --export-media option.
    """

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "output_file",
            help="Output file path for YAML dump of constance settings",
        )
        parser.add_argument(
            "--include-secrets",
            action="store_true",
            help="Include sensitive values (passwords, tokens) in the output",
        )
        parser.add_argument(
            "--include-defaults",
            action="store_true",
            help="Include settings that are set to their default values",
        )
        parser.add_argument(
            "--export-media",
            dest="media_dir",
            help="Export media files (logos, images) to this directory",
        )

    def handle(self, *args, **options):
        config = LazyConfig()
        output_file = options["output_file"]
        include_secrets = options["include_secrets"]
        include_defaults = options["include_defaults"]
        media_dir = options.get("media_dir")

        # Create media directory if requested
        if media_dir:
            os.makedirs(media_dir, exist_ok=True)
            self.stdout.write(
                self.style.SUCCESS(f"Created/using media directory: {media_dir}")
            )

        # Collect all settings
        all_settings = {}
        exported_files = []

        for name, options_tuple in settings.CONFIG.items():
            # Get current value
            value = getattr(config, name)

            # Get default value
            default = options_tuple[0]

            # Skip default values if not requested
            if not include_defaults and value == default:
                continue

            # Handle special cases
            if name in WHITELABELING_LOGOS:
                # For file/image fields, handle the export
                if value:
                    # Value is the filename stored in media
                    if media_dir:
                        # Export the actual file
                        try:
                            if default_storage.exists(value):
                                # Open the file from storage
                                with default_storage.open(value, "rb") as source_file:
                                    # Save to media_dir with same filename
                                    dest_path = os.path.join(media_dir, str(value))
                                    # Create subdirectories if the filename contains them
                                    os.makedirs(
                                        os.path.dirname(dest_path), exist_ok=True
                                    )
                                    with open(dest_path, "wb") as dest_file:
                                        shutil.copyfileobj(source_file, dest_file)
                                # Store the relative path for the YAML
                                all_settings[name] = os.path.join(media_dir, str(value))
                                exported_files.append(str(value))
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"File not found in storage for {name}: {value}"
                                    )
                                )
                                all_settings[name] = ""
                        except Exception as e:
                            self.stdout.write(
                                self.style.ERROR(
                                    f"Failed to export {name} file {value}: {e}"
                                )
                            )
                            all_settings[name] = ""
                    else:
                        # Just store the filename as is
                        all_settings[name] = str(value)
                else:
                    all_settings[name] = ""
            elif not include_secrets and (
                "password" in name.lower()
                or "token" in name.lower()
                or "secret" in name.lower()
                or "key" in name.lower()
            ):
                # Redact sensitive values unless explicitly requested
                all_settings[name] = "<redacted>"
            elif isinstance(value, dict | list):
                # For complex types, store as-is (YAML will handle serialization)
                all_settings[name] = value
            elif value is None:
                # Explicitly handle None values
                all_settings[name] = None
            elif isinstance(value, bool):
                # Ensure booleans are preserved
                all_settings[name] = value
            elif isinstance(value, int | float):
                # Preserve numeric types
                all_settings[name] = value
            else:
                # Convert everything else to string
                all_settings[name] = str(value)

        # Sort keys for consistent output
        sorted_settings = dict(sorted(all_settings.items()))

        # Write to YAML file
        try:
            with open(output_file, "w") as f:
                yaml.safe_dump(
                    sorted_settings,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,  # Already sorted above
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully dumped {len(sorted_settings)} settings to {output_file}"
                )
            )

            # Print statistics
            total_settings = len(settings.CONFIG)
            skipped_defaults = (
                total_settings - len(sorted_settings) if not include_defaults else 0
            )
            redacted_count = sum(
                1 for v in sorted_settings.values() if v == "<redacted>"
            )

            if skipped_defaults > 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipped {skipped_defaults} settings with default values (use --include-defaults to include them)"
                    )
                )

            if redacted_count > 0 and not include_secrets:
                self.stdout.write(
                    self.style.WARNING(
                        f"Redacted {redacted_count} sensitive settings (use --include-secrets to include them)"
                    )
                )

            if media_dir and exported_files:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Exported {len(exported_files)} media files to {media_dir}"
                    )
                )

        except OSError as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to write to file {output_file}: {e}")
            )
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Unexpected error: {e}"))
            return
