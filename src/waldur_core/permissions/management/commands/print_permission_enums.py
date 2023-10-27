from django.core.management.base import BaseCommand
from django.template.loader import render_to_string

from waldur_core.permissions import enums


class Command(BaseCommand):
    help = "Export permissions enums for HomePort"

    def handle(self, *args, **options):
        context = {
            'roles': enums.RoleEnum._member_map_.items(),
            'permissions': enums.PermissionEnum._member_map_.items(),
        }
        print(render_to_string('permissions/enums.ts', context).replace('\n\n', '\n'))
