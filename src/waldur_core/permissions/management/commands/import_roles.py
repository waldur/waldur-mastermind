import yaml
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role, RolePermission


class Command(BaseCommand):
    help = "Import roles configuration in YAML format"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "roles_file",
            help="Specifies location of roles configuration file.",
        )

    def handle(self, *args, **options):
        with open(options["roles_file"]) as auth_file:
            data = yaml.safe_load(auth_file)
            if data is None:
                return
            for row in data:
                try:
                    role = Role.objects.get(name=row["role"])
                except Role.DoesNotExist:
                    if "scope" not in row or row["scope"] not in TYPE_MAP:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Role {row['role']} has invalid scope field, skipping"
                            )
                        )
                        continue
                    content_type = ContentType.objects.get_by_natural_key(
                        *TYPE_MAP[row["scope"]]
                    )
                    role = Role.objects.create(
                        name=row["role"], content_type=content_type, is_system_role=True
                    )

                if not role.is_system_role:
                    role.is_system_role = True
                    role.save(update_fields=["is_system_role"])

                current_permissions = set(
                    RolePermission.objects.filter(role=role).values_list(
                        "permission", flat=True
                    )
                )
                if "permissions" not in row:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Role {row['role']} is missing permissions block, skipping"
                        )
                    )
                    continue
                new_permissions = set(row["permissions"])

                RolePermission.objects.filter(
                    role=role, permission__in=current_permissions - new_permissions
                ).delete()

                for permission in new_permissions - current_permissions:
                    RolePermission.objects.create(role=role, permission=permission)

                description = row.get("description")
                if description and role.description != description:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Updating description of role {row['role']} from {role.description} to {description}."
                        )
                    )
                    role.description = description
                    role.save()

                is_active = row.get("is_active")
                if is_active and role.is_active != is_active:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Updating is_active status of role {row['role']} from {role.is_active} to {is_active}."
                        )
                    )
                    role.is_active = is_active
                    role.save(update_fields=["is_active"])
