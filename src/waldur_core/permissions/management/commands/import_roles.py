import yaml
from django.core.management.base import BaseCommand

from waldur_core.permissions.models import Role, RolePermission


class Command(BaseCommand):
    help = "Import roles configuration in YAML format"

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
                current_permissions = set(
                    RolePermission.objects.filter(role=role).values_list('permission')
                )
                new_permissions = set(row['permissions'])

                RolePermission.objects.filter(
                    role=role, permission__in=current_permissions - new_permissions
                ).delete()

                for permission in new_permissions - current_permissions:
                    RolePermission.objects.create(role=role, permission=permission)

                description = row.get('description')
                if description and role.description != description:
                    role.description = description
                    role.save(update_fields=['description'])

                is_active = row.get('is_active')
                if is_active and role.is_active != is_active:
                    role.is_active = is_active
                    role.save(update_fields=['is_active'])
