import yaml
from django.core.management.base import BaseCommand

from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role


class Command(BaseCommand):
    help = """
    Export roles configuration to YAML format.

    This command exports all system roles or optionally only specific roles.
    The output format is compatible with the import_roles command.

    Usage:
        waldur export_roles roles.yaml
        waldur export_roles roles.yaml --system-only
        waldur export_roles roles.yaml --include-inactive
    """

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "output_file",
            help="Output file path for YAML export of roles configuration",
        )
        parser.add_argument(
            "--system-only",
            action="store_true",
            help="Export only system roles (default: all roles)",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include inactive roles in export (default: active only)",
        )
        parser.add_argument(
            "--role-names",
            nargs="*",
            help="Export only specific roles by name",
        )

    def handle(self, *args, **options):
        output_file = options["output_file"]
        system_only = options["system_only"]
        include_inactive = options["include_inactive"]
        role_names = options.get("role_names")

        # Build reverse mapping for TYPE_MAP to get scope names
        reverse_type_map = {}
        for scope_name, (app_label, model) in TYPE_MAP.items():
            reverse_type_map[f"{app_label}.{model}"] = scope_name

        # Build queryset
        queryset = Role.objects.all()

        if system_only:
            queryset = queryset.filter(is_system_role=True)

        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        if role_names:
            queryset = queryset.filter(name__in=role_names)

        # Prefetch related permissions for efficiency
        queryset = queryset.prefetch_related("permissions")

        # Export data
        roles_data = []
        exported_count = 0
        skipped_count = 0

        for role in queryset.order_by("name"):
            # Determine scope from content type
            content_type_key = (
                f"{role.content_type.app_label}.{role.content_type.model}"
            )
            scope = reverse_type_map.get(content_type_key)

            if not scope:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping role '{role.name}' - no scope mapping found for {content_type_key}"
                    )
                )
                skipped_count += 1
                continue

            # Get permissions
            permissions = list(
                role.permissions.values_list("permission", flat=True).order_by(
                    "permission"
                )
            )

            # Build role data
            role_data = {
                "role": role.name,
                "scope": scope,
                "permissions": permissions,
            }

            # Add optional fields if they differ from defaults
            if role.description:
                role_data["description"] = role.description

            if not role.is_active:
                role_data["is_active"] = role.is_active

            roles_data.append(role_data)
            exported_count += 1

        # Write to YAML file
        try:
            with open(output_file, "w") as f:
                yaml.safe_dump(
                    roles_data,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully exported {exported_count} roles to {output_file}"
                )
            )

            if skipped_count > 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipped {skipped_count} roles due to mapping issues"
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
