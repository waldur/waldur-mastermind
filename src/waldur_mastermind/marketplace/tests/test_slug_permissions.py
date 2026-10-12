"""Slug editing beyond staff: PROJECT.SET_SLUG and RESOURCE.SET_SLUG.

No role holds either permission by default, so out of the box only staff can
set a slug. A deployment grants them per role; the tests grant them the same
way, on the role, and check where the grant reaches and where it stops.
"""

import os
import tempfile

import yaml
from ddt import data, ddt
from django.conf import settings
from django.core.management import call_command
from rest_framework import serializers as rf_serializers
from rest_framework import status, test

from waldur_core.core.serializers import SlugSerializerMixin
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PERMISSION_DESCRIPTION, PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class GrantMixin:
    def grant(self, role, permission):
        role.add_permission(permission)
        self.addCleanup(lambda: role.delete_permission(permission))


@ddt
class ResourceSetSlugTest(GrantMixin, test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_id = ""
        self.resource.slug = "old-slug"
        self.resource.save()
        # Not provisioned yet: the factory's creation order still awaits approval.
        self.set_creation_order_state(OrderStates.PENDING_PROVIDER)

    def set_creation_order_state(self, state):
        self.resource.order_set.filter(type=OrderTypes.CREATE).update(state=state)

    def set_slug(self, user, slug="new-slug", resource=None, provider=False):
        resource = resource or self.resource
        self.client.force_authenticate(user)
        if provider:
            url = factories.ResourceFactory.get_provider_resource_url(
                resource, "set_slug"
            )
        else:
            url = factories.ResourceFactory.get_url(resource, "set_slug")
        return self.client.post(url, {"slug": slug})

    def test_staff_can_set_slug(self):
        response = self.set_slug(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.slug, "new-slug")

    @data("owner", "manager", "admin")
    def test_consumer_roles_cannot_set_slug_by_default(self, user):
        response = self.set_slug(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.slug, "old-slug")

    @data("offering_owner", "offering_manager", "service_manager")
    def test_provider_roles_cannot_set_slug_by_default(self, user):
        response = self.set_slug(getattr(self.fixture, user), provider=True)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_offering_manager_with_grant_sets_slug_of_own_offering_only(self):
        self.grant(OfferingRole.MANAGER, PermissionEnum.SET_RESOURCE_SLUG)
        manager = self.fixture.offering_manager

        response = self.set_slug(manager, provider=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        foreign = factories.ResourceFactory(
            offering=factories.OfferingFactory(), project=self.fixture.project
        )
        response = self.set_slug(manager, resource=foreign, provider=True)
        # Outside the grant the object is not visible at all.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        foreign.refresh_from_db()
        self.assertNotEqual(foreign.slug, "new-slug")

    def test_service_provider_manager_with_grant_sets_slug(self):
        self.grant(ServiceProviderRole.MANAGER, PermissionEnum.SET_RESOURCE_SLUG)
        response = self.set_slug(self.fixture.service_manager, provider=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_project_manager_with_grant_sets_slug(self):
        self.grant(ProjectRole.MANAGER, PermissionEnum.SET_RESOURCE_SLUG)
        response = self.set_slug(self.fixture.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_grant_on_one_organization_does_not_reach_another(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        other = fixtures.MarketplaceFixture()
        other.resource.backend_id = ""
        other.resource.save()

        response = self.set_slug(self.fixture.owner, resource=other.resource)

        # Outside the grant the object is not visible at all.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_duplicate_slug_is_rejected(self):
        factories.ResourceFactory(slug="taken")
        response = self.set_slug(self.fixture.staff, slug="taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)

    def test_holder_cannot_rename_after_backend_account_exists(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        self.resource.backend_id = "acc-1"
        self.resource.save()

        response = self.set_slug(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.slug, "old-slug")

    def test_staff_can_rename_after_backend_account_exists(self):
        self.resource.backend_id = "acc-1"
        self.resource.save()
        response = self.set_slug(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(OrderStates.EXECUTING, OrderStates.DONE, OrderStates.ERRED)
    def test_holder_cannot_rename_once_provisioning_started(self, state):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        self.set_creation_order_state(state)

        response = self.set_slug(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.slug, "old-slug")

    @data(OrderStates.PENDING_CONSUMER, OrderStates.PENDING_PROVIDER)
    def test_holder_can_rename_while_creation_order_is_pending(self, state):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        self.set_creation_order_state(state)
        response = self.set_slug(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_staff_can_rename_while_creation_order_is_executing(self):
        self.set_creation_order_state(OrderStates.EXECUTING)
        response = self.set_slug(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_change_is_logged_with_caller(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        self.set_slug(self.fixture.owner)

        event = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            message__contains="'slug'",
        ).latest("created")
        self.assertIn("old-slug", event.message)
        self.assertIn("new-slug", event.message)
        self.assertEqual(event.context["user_uuid"], self.fixture.owner.uuid.hex)


@ddt
class ProjectSetSlugTest(GrantMixin, test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.slug = "old-slug"
        self.project.save()
        self.fixture.resource.backend_id = ""
        self.fixture.resource.save()
        self.set_creation_order_state(OrderStates.PENDING_PROVIDER)

    def set_creation_order_state(self, state):
        self.fixture.resource.order_set.filter(type=OrderTypes.CREATE).update(
            state=state
        )

    def set_slug(self, user, slug="new-slug", project=None):
        self.client.force_authenticate(user)
        url = structure_factories.ProjectFactory.get_url(
            project or self.project, "set_slug"
        )
        return self.client.post(url, {"slug": slug})

    def test_staff_can_set_slug(self):
        response = self.set_slug(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.slug, "new-slug")

    @data("owner", "manager", "admin")
    def test_roles_cannot_set_slug_by_default(self, user):
        response = self.set_slug(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_with_grant_sets_slug_in_own_organization_only(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)

        response = self.set_slug(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        other_project = structure_factories.ProjectFactory()
        response = self.set_slug(
            self.fixture.owner, slug="other-slug", project=other_project
        )
        # Outside the grant the object is not visible at all.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_duplicate_slug_is_rejected(self):
        structure_factories.ProjectFactory(slug="taken")
        response = self.set_slug(self.fixture.staff, slug="taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)

    def test_holder_cannot_rename_once_a_resource_has_a_backend_account(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        self.fixture.resource.backend_id = "acc-1"
        self.fixture.resource.save()

        response = self.set_slug(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.project.refresh_from_db()
        self.assertEqual(self.project.slug, "old-slug")

    def test_holder_cannot_rename_once_a_resource_is_being_provisioned(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        self.set_creation_order_state(OrderStates.EXECUTING)

        response = self.set_slug(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.project.refresh_from_db()
        self.assertEqual(self.project.slug, "old-slug")

    def test_terminated_resources_do_not_lock_the_slug(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        self.fixture.resource.backend_id = "acc-1"
        self.fixture.resource.state = ResourceStates.TERMINATED
        self.fixture.resource.save()

        response = self.set_slug(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_staff_can_rename_once_a_resource_has_a_backend_account(self):
        self.fixture.resource.backend_id = "acc-1"
        self.fixture.resource.save()
        response = self.set_slug(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_change_is_logged_with_caller(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        self.set_slug(self.fixture.owner)

        event = Event.objects.filter(
            event_type="project_update_succeeded", message__contains="Slug"
        ).latest("created")
        self.assertIn("old-slug", event.message)
        self.assertIn("new-slug", event.message)
        self.assertEqual(event.context["user_uuid"], self.fixture.owner.uuid.hex)

    def test_patch_does_not_let_a_holder_change_the_slug(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        self.grant(CustomerRole.OWNER, PermissionEnum.UPDATE_PROJECT)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            structure_factories.ProjectFactory.get_url(self.project),
            {"name": "renamed", "slug": "patched"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "renamed")
        self.assertEqual(self.project.slug, "old-slug")


class ProjectCreateSlugTest(GrantMixin, test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.grant(CustomerRole.OWNER, PermissionEnum.CREATE_PROJECT)

    def create_project(self, user, slug):
        self.client.force_authenticate(user)
        return self.client.post(
            structure_factories.ProjectFactory.get_list_url(),
            {
                "name": "Project with slug",
                "customer": structure_factories.CustomerFactory.get_url(
                    self.fixture.customer
                ),
                "slug": slug,
            },
        )

    def test_slug_from_owner_without_grant_is_ignored(self):
        structure_factories.ProjectFactory(slug="taken")
        response = self.create_project(self.fixture.owner, "taken")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        project = structure_models.Project.objects.get(uuid=response.data["uuid"])
        self.assertNotEqual(project.slug, "taken")

    def test_malformed_slug_from_owner_without_grant_is_ignored(self):
        response = self.create_project(self.fixture.owner, "Bad Slug!")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(response.data["slug"], "Bad Slug!")

    def test_malformed_slug_from_owner_with_grant_is_rejected(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        response = self.create_project(self.fixture.owner, "Bad Slug!")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)

    def test_slug_from_owner_with_grant_is_used(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        response = self.create_project(self.fixture.owner, "chosen")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["slug"], "chosen")

    def test_duplicate_slug_from_owner_with_grant_is_rejected(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_PROJECT_SLUG)
        structure_factories.ProjectFactory(slug="taken")
        response = self.create_project(self.fixture.owner, "taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)

    def test_slug_from_staff_is_used(self):
        response = self.create_project(self.fixture.staff, "chosen")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["slug"], "chosen")


class OrderCreateSlugTest(GrantMixin, test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.grant(CustomerRole.OWNER, PermissionEnum.CREATE_ORDER)

    def create_order(self, user, slug):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.OrderFactory.get_list_url(),
            {
                "offering": factories.OfferingFactory.get_public_url(
                    self.fixture.offering
                ),
                "project": structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                "plan": factories.PlanFactory.get_public_url(self.fixture.plan),
                "attributes": {"name": "resource-with-slug"},
                "slug": slug,
            },
            format="json",
        )

    def created_resource(self, response):
        return models.Order.objects.get(uuid=response.data["uuid"]).resource

    def test_slug_from_staff_names_the_resource(self):
        response = self.create_order(self.fixture.staff, "chosen")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.created_resource(response).slug, "chosen")

    def test_slug_from_owner_without_grant_is_ignored(self):
        factories.ResourceFactory(slug="taken")
        response = self.create_order(self.fixture.owner, "taken")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(self.created_resource(response).slug, "taken")

    def test_too_long_slug_from_owner_without_grant_is_ignored(self):
        response = self.create_order(self.fixture.owner, "x" * 60)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(self.created_resource(response).slug, "x" * 60)

    def test_too_long_slug_from_owner_with_grant_is_rejected(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        response = self.create_order(self.fixture.owner, "x" * 60)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)

    def test_slug_from_owner_with_grant_names_the_resource(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        response = self.create_order(self.fixture.owner, "chosen")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.created_resource(response).slug, "chosen")

    def test_duplicate_resource_slug_is_rejected(self):
        self.grant(CustomerRole.OWNER, PermissionEnum.SET_RESOURCE_SLUG)
        factories.ResourceFactory(slug="taken")
        response = self.create_order(self.fixture.owner, "taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)

    def test_duplicate_resource_slug_from_staff_is_rejected(self):
        factories.ResourceFactory(slug="taken")
        response = self.create_order(self.fixture.staff, "taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data)


class SlugPermissionDeploymentTest(test.APITestCase):
    """The deploy sequence: import_roles resets system roles, then
    override_roles applies the deployment's additions."""

    def deploy(self, override_file):
        call_command(
            "import_roles",
            os.path.join(
                settings.BASE_DIR, "docker/rootfs/etc/waldur/permissions.yaml"
            ),
        )
        call_command("override_roles", override_file)

    def test_no_role_holds_slug_permissions_after_import(self):
        call_command(
            "import_roles",
            os.path.join(
                settings.BASE_DIR, "docker/rootfs/etc/waldur/permissions.yaml"
            ),
        )
        self.assertFalse(
            RolePermission.objects.filter(
                role__is_system_role=True,
                permission__in=[
                    PermissionEnum.SET_RESOURCE_SLUG,
                    PermissionEnum.SET_PROJECT_SLUG,
                ],
            ).exists()
        )

    def test_override_grant_survives_redeploy(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(
                [
                    {
                        "role": "OFFERING.MANAGER",
                        "add_permissions": [PermissionEnum.SET_RESOURCE_SLUG.value],
                    }
                ],
                f,
            )
        self.addCleanup(os.unlink, f.name)

        self.deploy(f.name)
        self.deploy(f.name)

        self.assertTrue(
            RolePermission.objects.filter(
                role=Role.objects.get(name="OFFERING.MANAGER"),
                permission=PermissionEnum.SET_RESOURCE_SLUG,
            ).exists()
        )


class SlugPermissionLabelTest(test.APITestCase):
    def test_resource_slug_permission_is_listed_as_a_provider_action(self):
        groups = {
            option["value"]: group["label"]
            for group in PERMISSION_DESCRIPTION
            for option in group["options"]
        }
        self.assertEqual(groups[PermissionEnum.SET_RESOURCE_SLUG], "Provider actions")


class SlugSerializerMixinTest(test.APITestCase):
    def test_slug_permission_without_scope_hook_fails_at_definition(self):
        with self.assertRaises(TypeError):

            class Forgetful(SlugSerializerMixin, rf_serializers.ModelSerializer):
                slug_create_permission = PermissionEnum.SET_PROJECT_SLUG

                class Meta:
                    model = structure_models.Project
                    fields = ("name", "slug")
