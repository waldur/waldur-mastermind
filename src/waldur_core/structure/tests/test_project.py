import datetime
import uuid
from unittest import mock

from ddt import data, ddt
from django.test import TransactionTestCase
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import executors, models, permissions
from waldur_core.structure.models import Project
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests import models as test_models
from waldur_core.structure.utils import move_project
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ProjectPermissionGrantTest(TransactionTestCase):
    def setUp(self):
        self.project = factories.ProjectFactory()
        self.user = factories.UserFactory()

    def test_add_user_returns_permission(self):
        permission = self.project.add_user(self.user, ProjectRole.ADMIN)

        self.assertEqual(permission.user, self.user)
        self.assertEqual(permission.scope, self.project)


@ddt
class ProjectUpdateDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_PROJECT)

    # Update tests:
    def test_user_can_change_single_project_field(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {"name": "New project name"}
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.fixture.project), data
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("New project name", response.data["name"])
        self.assertTrue(Project.objects.filter(name=data["name"]).exists())

    def test_update_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {"backend_id": "backend_id"}
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.fixture.project), data
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("backend_id", response.data["backend_id"])
        self.assertTrue(Project.objects.filter(backend_id=data["backend_id"]).exists())

    @data("staff", "owner")
    def test_user_can_update_end_date(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        with freeze_time("2020-01-01"):
            data = {"end_date": "2021-01-01"}
            response = self.client.patch(
                factories.ProjectFactory.get_url(self.fixture.project), data
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.fixture.project.refresh_from_db()
            self.assertTrue(self.fixture.project.end_date)
            self.assertEqual(
                self.fixture.project.end_date_requested_by, getattr(self.fixture, user)
            )

    @data("manager", "admin")
    def test_user_cannot_update_end_date(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        with freeze_time("2020-01-01"):
            data = {"end_date": "2021-01-01"}
            response = self.client.patch(
                factories.ProjectFactory.get_url(self.fixture.project), data
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.fixture.project.refresh_from_db()
            self.assertFalse(self.fixture.project.end_date)

    # Delete tests:
    def test_user_can_delete_project_belonging_to_the_customer_he_owns(self):
        self.client.force_authenticate(self.fixture.owner)

        project = self.fixture.project
        response = self.client.delete(factories.ProjectFactory.get_url(project))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Project.available_objects.filter(pk=project.pk).exists())

    def test_soft_delete(self):
        project = self.fixture.project
        pk = project.pk
        project.delete()
        self.assertFalse(Project.available_objects.filter(pk=pk).exists())
        self.assertTrue(Project.objects.filter(pk=pk).exists())

    @override_waldur_core_settings(OECD_FOS_2007_CODE_MANDATORY=True)
    def test_update_if_oecd_is_not_passed(self):
        self.fixture.project.save()
        self.client.force_authenticate(self.fixture.staff)

        data = {"backend_id": "backend_id"}
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.fixture.project), data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class ProjectCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_PROJECT)

    def test_staff_can_create_any_project(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(self.fixture.customer)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Project.objects.filter(name=data["name"]).exists())

    def test_owner_can_create_project_belonging_to_the_customer_he_owns(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(self.fixture.customer)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Project.objects.filter(name=data["name"]).exists())

    def test_owner_cannot_create_project_not_belonging_to_the_customer_he_owns(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(factories.CustomerFactory())
        data["name"] = "unique name 2"

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Project.objects.filter(name=data["name"]).exists())

    def test_customer_support_cannot_create_project(self):
        self.client.force_authenticate(self.fixture.customer_support)
        data = self._get_valid_project_payload(self.fixture.customer)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Project.objects.filter(name=data["name"]).exists())

    def test_validate_end_date(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["end_date"] = "2021-06-01"

        with freeze_time("2021-07-01"):
            response = self.client.post(
                factories.ProjectFactory.get_list_url(), payload
            )

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertTrue(
                "Cannot be earlier than the current date." in str(response.data)
            )
            self.assertFalse(Project.objects.filter(name=payload["name"]).exists())

        with freeze_time("2021-06-01"):
            response = self.client.post(
                factories.ProjectFactory.get_list_url(), payload
            )

            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertTrue(
                Project.objects.filter(
                    name=payload["name"],
                    end_date=datetime.datetime(year=2021, month=6, day=1).date(),
                ).exists()
            )

    @data("staff", "owner")
    def test_user_can_set_end_date(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["end_date"] = "2021-06-01"

        with freeze_time("2021-01-01"):
            response = self.client.post(
                factories.ProjectFactory.get_list_url(), payload
            )

            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertTrue(
                Project.objects.filter(
                    name=payload["name"],
                    end_date=datetime.datetime(year=2021, month=6, day=1).date(),
                ).exists()
            )
            project = Project.objects.get(
                name=payload["name"],
                end_date=datetime.datetime(year=2021, month=6, day=1).date(),
            )
            self.assertEqual(project.end_date_requested_by, getattr(self.fixture, user))

    @data("manager", "admin")
    def test_user_cannot_set_end_date(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["end_date"] = "2021-06-01"

        with freeze_time("2021-01-01"):
            response = self.client.post(
                factories.ProjectFactory.get_list_url(), payload
            )

            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_oecd_fos_2007_code(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["oecd_fos_2007_code"] = "1.1"
        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual("1.1", response.data["oecd_fos_2007_code"])

    @override_waldur_core_settings(OECD_FOS_2007_CODE_MANDATORY=True)
    def test_oecd_fos_2007_code_is_required(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "new"
        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def _get_valid_project_payload(self, customer):
        return {
            "name": "New project name",
            "customer": factories.CustomerFactory.get_url(customer),
        }


class ProjectApiPermissionTest(test.APITransactionTestCase):
    forbidden_combinations = (
        # User role, Project
        ("admin", "manager"),
        ("admin", "inaccessible"),
        ("manager", "admin"),
        ("manager", "inaccessible"),
        ("no_role", "admin"),
        ("no_role", "manager"),
        ("no_role", "inaccessible"),
    )

    def setUp(self):
        self.users = {
            "owner": factories.UserFactory(),
            "admin": factories.UserFactory(),
            "manager": factories.UserFactory(),
            "no_role": factories.UserFactory(),
            "multirole": factories.UserFactory(),
        }

        self.projects = {
            "owner": factories.ProjectFactory(),
            "admin": factories.ProjectFactory(),
            "manager": factories.ProjectFactory(),
            "inaccessible": factories.ProjectFactory(),
        }

        self.projects["admin"].add_user(self.users["admin"], ProjectRole.ADMIN)
        self.projects["manager"].add_user(self.users["manager"], ProjectRole.MANAGER)

        self.projects["admin"].add_user(self.users["multirole"], ProjectRole.ADMIN)
        self.projects["manager"].add_user(self.users["multirole"], ProjectRole.MANAGER)
        self.projects["owner"].customer.add_user(
            self.users["owner"], CustomerRole.OWNER
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)

    # TODO: Test for customer owners
    # Creation tests
    def test_anonymous_user_cannot_create_project(self):
        for old_project in self.projects.values():
            project = factories.ProjectFactory(customer=old_project.customer)
            response = self.client.post(
                reverse("project-list"), self._get_valid_payload(project)
            )
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_cannot_create_project_within_customer_he_doesnt_own_but_admins_its_project(
        self,
    ):
        self.client.force_authenticate(user=self.users["admin"])

        customer = self.projects["admin"].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(
            reverse("project-list"), self._get_valid_payload(project)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertDictContainsSubset(
            {"detail": "You do not have permission to perform this action."},
            response.data,
        )

    def test_user_cannot_create_project_within_customer_he_doesnt_own_but_manages_its_project(
        self,
    ):
        self.client.force_authenticate(user=self.users["manager"])

        customer = self.projects["manager"].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(
            reverse("project-list"), self._get_valid_payload(project)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertDictContainsSubset(
            {"detail": "You do not have permission to perform this action."},
            response.data,
        )

    def test_user_cannot_create_project_within_customer_he_is_not_affiliated_with(self):
        self.client.force_authenticate(user=self.users["admin"])

        project = factories.ProjectFactory()
        response = self.client.post(
            reverse("project-list"), self._get_valid_payload(project)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertDictContainsSubset(
            {"customer": ["Invalid hyperlink - Object does not exist."]}, response.data
        )

    def test_user_can_create_project_within_customer_he_owns(self):
        self.client.force_authenticate(user=self.users["owner"])

        customer = self.projects["owner"].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(
            reverse("project-list"), self._get_valid_payload(project)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_user_can_create_project(self):
        staff = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        customer = self.projects["inaccessible"].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(
            reverse("project-list"), self._get_valid_payload(project)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # List filtration tests
    def test_anonymous_user_cannot_list_projects(self):
        response = self.client.get(reverse("project-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_list_projects_belonging_to_customer_he_owns(self):
        self._ensure_list_access_allowed("owner")

    def test_user_can_list_projects_he_is_administrator_of(self):
        self._ensure_list_access_allowed("admin")

    def test_user_can_list_projects_he_is_manager_of(self):
        self._ensure_list_access_allowed("manager")

    def test_user_cannot_list_projects_he_has_no_role_in(self):
        for user_role, project in self.forbidden_combinations:
            self._ensure_list_access_forbidden(user_role, project)

    def test_user_can_filter_by_projects_where_he_has_manager_role(self):
        self.client.force_authenticate(user=self.users["multirole"])
        response = self.client.get(reverse("project-list") + "?can_manage")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        managed_project_url = self._get_project_url(self.projects["manager"])
        administrated_project_url = self._get_project_url(self.projects["admin"])

        self.assertIn(
            managed_project_url, [resource["url"] for resource in response.data]
        )
        self.assertNotIn(
            administrated_project_url, [resource["url"] for resource in response.data]
        )

    # Direct instance access tests
    def test_anonymous_user_cannot_access_project(self):
        project = factories.ProjectFactory()
        response = self.client.get(self._get_project_url(project))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_access_project_belonging_to_customer_he_owns(self):
        self._ensure_direct_access_allowed("owner")

    def test_user_can_access_project_he_is_administrator_of(self):
        self._ensure_direct_access_allowed("admin")

    def test_user_can_access_project_he_is_manager_of(self):
        self._ensure_direct_access_allowed("manager")

    def test_user_cannot_access_project_he_has_no_role_in(self):
        for user_role, project in self.forbidden_combinations:
            self._ensure_direct_access_forbidden(user_role, project)

    # Helper methods
    def _get_project_url(self, project):
        return factories.ProjectFactory.get_url(project)

    def _get_valid_payload(self, resource=None):
        resource = resource or factories.ProjectFactory()
        return {
            "name": resource.name,
            "customer": factories.CustomerFactory.get_url(resource.customer),
        }

    def _ensure_list_access_allowed(self, user_role):
        self.client.force_authenticate(user=self.users[user_role])

        response = self.client.get(reverse("project-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        project_url = self._get_project_url(self.projects[user_role])
        self.assertIn(project_url, [instance["url"] for instance in response.data])

    def _ensure_list_access_forbidden(self, user_role, project):
        self.client.force_authenticate(user=self.users[user_role])

        response = self.client.get(reverse("project-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        project_url = self._get_project_url(self.projects[project])
        self.assertNotIn(project_url, [resource["url"] for resource in response.data])

    def _ensure_direct_access_allowed(self, user_role):
        self.client.force_authenticate(user=self.users[user_role])
        response = self.client.get(self._get_project_url(self.projects[user_role]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def _ensure_direct_access_forbidden(self, user_role, project):
        self.client.force_authenticate(user=self.users[user_role])

        response = self.client.get(self._get_project_url(self.projects[project]))
        # 404 is used instead of 403 to hide the fact that the resource exists at all
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ProjectCountersListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.owner = self.fixture.owner
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.project = self.fixture.project
        self.url = factories.ProjectFactory.get_url(self.project, action="counters")

    def test_user_can_get_project_counters(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"users": 2})


class TestExecutor(executors.BaseCleanupExecutor):
    pre_models = (test_models.TestNewInstance,)


@mock.patch("waldur_core.core.WaldurExtension.get_extensions")
class ProjectCleanupTest(test.APITransactionTestCase):
    def test_executors_are_sorted_in_topological_order(self, get_extensions):
        class ParentExecutor(executors.BaseCleanupExecutor):
            pass

        class ParentExtension:
            @staticmethod
            def get_cleanup_executor():
                return ParentExecutor

        class ChildExecutor(executors.BaseCleanupExecutor):
            related_executor = ParentExecutor

        class ChildExtension:
            @staticmethod
            def get_cleanup_executor():
                return ChildExecutor

        get_extensions.return_value = [ParentExtension, ChildExtension]

        self.assertEqual(
            [ChildExecutor, ParentExecutor],
            executors.ProjectCleanupExecutor.get_executors(),
        )

    def test_project_without_resources_is_deleted(self, get_extensions):
        fixture = fixtures.ServiceFixture()
        project = fixture.project

        get_extensions.return_value = []
        executors.ProjectCleanupExecutor.execute(fixture.project, is_async=False)

        self.assertFalse(
            models.Project.available_objects.filter(id=project.id).exists()
        )

    def test_project_with_resources_and_executors_is_deleted(self, get_extensions):
        fixture = fixtures.ServiceFixture()
        project = fixture.project
        resource = fixture.resource

        class TestExtension:
            @staticmethod
            def get_cleanup_executor():
                return TestExecutor

        get_extensions.return_value = [TestExtension]
        executors.ProjectCleanupExecutor.execute(fixture.project, is_async=False)

        self.assertFalse(
            models.Project.available_objects.filter(id=project.id).exists()
        )
        self.assertFalse(
            test_models.TestNewInstance.objects.filter(id=resource.id).exists()
        )


class ChangeProjectCustomerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.old_customer = self.project.customer
        self.new_customer = factories.CustomerFactory()

    def change_customer(self):
        move_project(self.project, self.new_customer)
        self.project.refresh_from_db()

    def test_change_customer(self):
        self.change_customer()
        self.assertEqual(self.new_customer, self.project.customer)

    def test_if_project_customer_has_been_changed_then_users_permissions_must_be_deleted(
        self,
    ):
        self.fixture.admin
        self.change_customer()
        self.assertFalse(
            permissions._has_admin_access(self.fixture.admin, self.project)
        )

    def test_recalculate_quotas(self):
        self.assertEqual(self.old_customer.get_quota_usage("nc_project_count"), 1.0)
        self.assertEqual(self.new_customer.get_quota_usage("nc_project_count"), 0)
        self.change_customer()
        self.assertEqual(self.old_customer.get_quota_usage("nc_project_count"), 0)
        self.assertEqual(self.new_customer.get_quota_usage("nc_project_count"), 1.0)


@ddt
class ChangeProjectImageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.url = factories.ProjectFactory.get_url(self.project)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT)

    @data("staff", "owner", "manager")
    def test_user_can_update_image(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        self.assertFalse(self.project.image)
        response = self.client.patch(
            self.url, {"image": dummy_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertTrue(self.project.image)

    @data("admin", "customer_support", "member", "global_support")
    def test_user_cannot_update_image(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        self.assertFalse(self.project.image)
        response = self.client.patch(
            self.url, {"image": dummy_image()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProjectMoveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.url = factories.ProjectFactory.get_url(self.project, action="move_project")
        self.customer = factories.CustomerFactory()

    def get_response(self, role, customer):
        self.client.force_authenticate(role)
        payload = {"customer": {"url": factories.CustomerFactory.get_url(customer)}}
        return self.client.post(self.url, payload)

    def test_move_project_rest(self):
        response = self.get_response(self.fixture.staff, self.customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.project.customer, self.customer)

    def test_move_project_is_not_possible_when_customer_the_same(self):
        old_customer = self.project.customer
        response = self.get_response(self.fixture.staff, old_customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.project.customer, old_customer)

    def test_move_project_is_not_possible_when_new_customer_is_blocked(self):
        old_customer = self.project.customer
        self.customer.blocked = True
        self.customer.save(update_fields=["blocked"])
        response = self.get_response(self.fixture.staff, self.customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.project.customer, old_customer)


class ProjectListFilterTest(test.APITransactionTestCase):
    _valid_backend_id = uuid.uuid4()
    _valid_effective_id = uuid.uuid4()

    def setUp(self):
        self.user_fixture = fixtures.UserFixture()
        self.project1 = factories.ProjectFactory(name="project_1")
        self.project2 = factories.ProjectFactory(name="project_2")

        offering = marketplace_factories.OfferingFactory()
        self.resource1 = marketplace_factories.ResourceFactory(
            project=self.project1,
            offering=offering,
            effective_id=ProjectListFilterTest._valid_effective_id,
            backend_id=ProjectListFilterTest._valid_backend_id,
        )
        self.resource2 = marketplace_factories.ResourceFactory(
            project=self.project2,
            offering=offering,
            name="resource_2",
            backend_id="non_uuid_backend_id",
            effective_id="non_uuid_effective_id",
        )
        self.url = factories.ProjectFactory.get_list_url()

    def test_filter_projects_by_uuid_like_resource_effective_id(self):
        self.client.force_authenticate(self.user_fixture.staff)
        response = self.client.get(
            self.url, {"query": ProjectListFilterTest._valid_effective_id}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.project1.name)

    def test_filter_projects_by_uuid_like_resource_backend_id(self):
        self.client.force_authenticate(self.user_fixture.staff)
        response = self.client.get(
            self.url, {"query": ProjectListFilterTest._valid_backend_id}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.project1.name)

    def test_filter_projects_by_non_uuid_like_resource_effective_id(self):
        self.client.force_authenticate(self.user_fixture.staff)
        response = self.client.get(self.url, {"query": "non_uuid_effective_id"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.project2.name)

    def test_filter_projects_by_non_uuid_like_resource_backend_id(self):
        self.client.force_authenticate(self.user_fixture.staff)
        response = self.client.get(self.url, {"query": "non_uuid_backend_id"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.project2.name)

    def test_filter_projects_by_resource_name(self):
        self.client.force_authenticate(self.user_fixture.staff)
        response = self.client.get(self.url, {"query": "resource_2"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.project2.name)

    def test_filter_projects_by_name(self):
        self.client.force_authenticate(self.user_fixture.staff)
        response = self.client.get(self.url, {"query": "project_1"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.project1.name)
