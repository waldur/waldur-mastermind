import yaml
from django.core.management.base import BaseCommand

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
            'roles_file',
            help='Specifies location of roles configuration file.',
        )

    def handle(self, *args, **options):
        with open(options['roles_file']) as auth_file:
            data = yaml.safe_load(auth_file)
        if data is None:
            return

        for row in data:
            role = Role.objects.get(name=row['role'])
            description = row.get('description')
            permissions_add = row.get('add_permissions')
            permissions_drop = row.get('drop_permissions')

            if description is not None and description != role.description:
                role.description = description
                role.save(update_fields=['description'])

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

            is_active = row.get('is_active')
            if is_active and role.is_active != is_active:
                self.stdout.write(
                    self.style.WARNING(
                        f'Updating is_active status of role {row["role"]} from {role.is_active} to {is_active}.'
                    )
                )
                role.is_active = is_active
                role.save(update_fields=['is_active'])
