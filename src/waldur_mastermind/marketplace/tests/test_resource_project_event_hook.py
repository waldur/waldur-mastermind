"""Regression test for ResourceProject + active event hook.

Without ``StructureLoggableMixin`` on ``ResourceProject`` the role-granted
event handler crashes with ``AttributeError: type object 'ResourceProject'
has no attribute 'get_permitted_objects'`` whenever a user has subscribed
an EmailHook/WebHook to the role event types
(see waldur_core/logging/tasks.py:99).
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class AddUserWithActiveHookTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.staff = self.fixture.staff
        self.resource_project = models.ResourceProject.objects.create(
            resource=self.fixture.resource, name="Project A"
        )
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        self.rp_role = Role.objects.create(
            name="Project Member", content_type=rp_ct, is_system_role=False
        )
        self.invitee = structure_factories.UserFactory()

        # Active hook subscribed to the role-granted event flow. Triggers
        # check_event() → ResourceProject.get_permitted_objects(hook.user).
        logging_models.EmailHook.objects.create(
            user=self.staff,
            email=self.staff.email,
            event_types=[EventType.ROLE_GRANTED],
            is_active=True,
        )

    def test_add_user_does_not_crash_when_event_hook_is_active(self):
        self.client.force_authenticate(self.staff)
        url = (
            f"/api/marketplace-resource-projects/{self.resource_project.uuid.hex}"
            "/add_user/"
        )
        response = self.client.post(
            url,
            {"user": self.invitee.uuid.hex, "role": self.rp_role.name},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_get_permitted_objects_scopes_by_parent_project(self):
        # Staff sees everything.
        self.assertIn(
            self.resource_project,
            models.ResourceProject.get_permitted_objects(self.staff),
        )
        # Unrelated user sees nothing.
        outsider = structure_factories.UserFactory()
        self.assertNotIn(
            self.resource_project,
            models.ResourceProject.get_permitted_objects(outsider),
        )
