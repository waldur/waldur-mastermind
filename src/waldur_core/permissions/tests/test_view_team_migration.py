from importlib import import_module

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure.models import Customer

migration = import_module(
    "waldur_core.permissions.migrations.0029_view_team_permissions"
)


class ViewTeamMigrationTest(test.APITestCase):
    def test_system_roles_and_their_clones_get_the_permission(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        owner, _ = Role.objects.get_or_create(
            name="CUSTOMER.OWNER",
            content_type=customer_ct,
            defaults={"is_system_role": True},
        )
        clone = Role.objects.create(
            name="CUSTOMER.acme.OWNER", content_type=customer_ct, template=owner
        )
        unrelated = Role.objects.create(
            name="CUSTOMER.acme.CUSTOM", content_type=customer_ct
        )
        RolePermission.objects.filter(permission="CUSTOMER.VIEW_TEAM").delete()

        migration.add_view_team_permissions(apps, None)
        migration.add_view_team_permissions(apps, None)  # idempotent

        with_permission = set(
            RolePermission.objects.filter(permission="CUSTOMER.VIEW_TEAM").values_list(
                "role__name", flat=True
            )
        )
        self.assertIn(owner.name, with_permission)
        self.assertIn(clone.name, with_permission)
        self.assertNotIn(unrelated.name, with_permission)
        self.assertEqual(
            RolePermission.objects.filter(
                role=clone, permission="CUSTOMER.VIEW_TEAM"
            ).count(),
            1,
        )
