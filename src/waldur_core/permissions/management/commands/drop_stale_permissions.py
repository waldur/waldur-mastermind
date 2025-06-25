from django.core.management.base import BaseCommand

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import RolePermission


class Command(BaseCommand):
    help = """
        Delete permissions from DB which are no longer in code.
    """

    def handle(self, *args, **options):
        db_permissions = RolePermission.objects.all()
        code_permissions_raw = list(PermissionEnum)
        code_permissions = {permission.value for permission in code_permissions_raw}
        for db_permission in db_permissions:
            if db_permission.permission not in code_permissions:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Deleting permission {db_permission.permission} from DB."
                    )
                )
                db_permission.delete()
