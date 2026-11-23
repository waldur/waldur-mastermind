import yaml
from django.core.management.base import BaseCommand

from waldur_core.permissions.enums import NARROWER_PERMISSIONS
from waldur_core.permissions.models import Role, RolePermission


class Command(BaseCommand):
    help = """
        Override roles configuration in YAML format. The example of roles-override.yaml:

        - role: CUSTOMER.OWNER
          description: "Custom owner role"
          is_active: True
          add_permissions:
            - OFFERING.CREATE
            - OFFERING.DELETE
            - OFFERING.UPDATE
          drop_permissions:
            - OFFERING.UPDATE_THUMBNAIL
            - OFFERING.UPDATE_ATTRIBUTES
    """

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
            role = Role.objects.get(name=row["role"])
            description = row.get("description")
            permissions_add = row.get("add_permissions")
            permissions_drop = row.get("drop_permissions")

            if description is not None and description != role.description:
                self.stdout.write(
                    self.style.WARNING(
                        f"Updating description of role {row['role']} from {role.description} to {description}."
                    )
                )
                role.description = description
                role.save()

            if permissions_add:
                for permission in permissions_add:
                    role.add_permission(permission)

            if permissions_drop:
                for permission in permissions_drop:
                    existing_permission = RolePermission.objects.filter(
                        role=role, permission=permission
                    ).first()
                    if existing_permission:
                        existing_permission.delete()
                self._drop_orphaned_narrower(role, permissions_drop, permissions_add)

            is_active = row.get("is_active")
            if is_active is not None and role.is_active != is_active:
                self.stdout.write(
                    self.style.WARNING(
                        f"Updating is_active status of role {row['role']} from {role.is_active} to {is_active}."
                    )
                )
                role.is_active = is_active
                role.save()

    def _drop_orphaned_narrower(self, role, dropped, added):
        """Drop a narrower permission once the role holds none of the broader
        ones it was split out of, unless the entry adds it back."""
        held = set(role.permissions.values_list("permission", flat=True))
        for permission in dropped:
            for narrower in NARROWER_PERMISSIONS.get(permission, ()):
                if narrower in (added or ()) or narrower not in held:
                    continue
                broader = {
                    candidate
                    for candidate, narrowers in NARROWER_PERMISSIONS.items()
                    if narrower in narrowers
                }
                if broader & held:
                    continue
                RolePermission.objects.filter(role=role, permission=narrower).delete()
                held.discard(narrower)
                self.stdout.write(
                    self.style.WARNING(
                        f"Dropping {narrower} from role {role.name}: it holds "
                        f"none of {', '.join(sorted(broader))} any more. List it "
                        "under add_permissions to keep it."
                    )
                )
