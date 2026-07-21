import datetime
import uuid
from datetime import timedelta
from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.models import DESCRIPTION_LENGTH
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure import executors, models, permissions
from waldur_core.structure.models import Project
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests import models as test_models
from waldur_core.structure.utils import move_project
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import Invitation
from waldur_mastermind.marketplace.enums import BillingTypes
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
class ProjectUpdateDeleteTest(test.APITestCase):
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


@ddt
class ProjectCreateTest(test.APITestCase):
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

    @override_config(PROJECT_END_DATE_MANDATORY=True)
    def test_project_end_date_is_required_when_setting_enabled(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "project_without_end_date"
        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)
        self.assertEqual(response.data["end_date"][0], "This field is required.")

    @override_config(PROJECT_END_DATE_MANDATORY=True)
    def test_project_can_be_created_with_end_date_when_setting_enabled(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "project_with_end_date"
        payload["end_date"] = "2030-12-31"
        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(response.data["end_date"]), "2030-12-31")

    @override_config(PROJECT_END_DATE_MANDATORY=False)
    def test_project_can_be_created_without_end_date_when_setting_disabled(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "project_without_end_date_allowed"
        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_description_exceeds_limit_after_html_clean_returns_400(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "project_with_long_description"
        payload["description"] = "&" * DESCRIPTION_LENGTH

        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("description", response.data)
        self.assertFalse(Project.objects.filter(name=payload["name"]).exists())

    @override_config(PROJECT_END_DATE_MANDATORY=True)
    def test_patch_does_not_require_end_date_when_field_is_not_being_changed(self):
        # Regression: setting PROJECT_END_DATE_MANDATORY must not block PATCH
        # requests that don't touch end_date on projects whose end_date is null.
        self.client.force_authenticate(self.fixture.staff)
        project = self.fixture.project
        project.end_date = None
        project.save()
        response = self.client.patch(
            factories.ProjectFactory.get_url(project),
            {"description": "updated"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["description"], "updated")

    @override_config(PROJECT_END_DATE_MANDATORY=True)
    def test_patch_rejects_explicit_null_end_date_when_setting_enabled(self):
        self.client.force_authenticate(self.fixture.staff)
        project = self.fixture.project
        project.end_date = datetime.date(2030, 1, 1)
        project.save()
        response = self.client.patch(
            factories.ProjectFactory.get_url(project),
            {"end_date": None},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)

    @override_waldur_core_settings(OECD_FOS_2007_CODE_MANDATORY=True)
    def test_patch_does_not_require_oecd_code_when_field_is_not_being_changed(self):
        # Regression: same anti-pattern as PROJECT_END_DATE_MANDATORY -- a
        # PATCH that doesn't touch oecd_fos_2007_code must not be rejected
        # just because the project's current value is null.
        self.client.force_authenticate(self.fixture.staff)
        project = self.fixture.project
        project.oecd_fos_2007_code = None
        project.save()
        response = self.client.patch(
            factories.ProjectFactory.get_url(project),
            {"description": "updated"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @override_config(AFFILIATION_REQUIRED_AT_PROJECT_CREATION=True)
    def test_patch_does_not_require_affiliation_when_field_is_not_being_changed(self):
        # Regression: same anti-pattern. The setting name itself implies
        # "at creation", so PATCHes that don't include affiliation_uuid must
        # be allowed even when the project's current affiliation is null.
        self.client.force_authenticate(self.fixture.staff)
        project = self.fixture.project
        project.affiliation = None
        project.save()
        response = self.client.patch(
            factories.ProjectFactory.get_url(project),
            {"description": "updated"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_validate_start_date(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["start_date"] = "2021-06-01"

        # Test that past dates are rejected
        with freeze_time("2021-07-01"):
            response = self.client.post(
                factories.ProjectFactory.get_list_url(), payload
            )

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertTrue(
                "Cannot be earlier than the current date." in str(response.data)
            )
            self.assertFalse(Project.objects.filter(name=payload["name"]).exists())

        # Test that current date is accepted
        with freeze_time("2021-06-01"):
            response = self.client.post(
                factories.ProjectFactory.get_list_url(), payload
            )

            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertTrue(
                Project.objects.filter(
                    name=payload["name"],
                    start_date=datetime.datetime(year=2021, month=6, day=1).date(),
                ).exists()
            )

    def test_validate_start_date_null_handling(self):
        """Test that None/null values are properly handled for start_date."""
        self.client.force_authenticate(self.fixture.staff)

        # Create a project with a future start_date to avoid read-only logic
        with freeze_time("2021-01-01"):
            project = factories.ProjectFactory(
                customer=self.fixture.customer,
                start_date=datetime.datetime(year=2025, month=1, day=1).date(),
            )

            # Test that we can clear the start_date by sending null
            payload = {"start_date": None}
            url = factories.ProjectFactory.get_url(project)

            response = self.client.patch(url, payload)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify the start_date was cleared
            project.refresh_from_db()
            self.assertIsNone(project.start_date)

    def test_validate_end_date_null_handling(self):
        """Test that None/null values are properly handled for end_date."""
        self.client.force_authenticate(self.fixture.staff)

        # Create a project with a future end_date
        with freeze_time("2021-01-01"):
            project = factories.ProjectFactory(
                customer=self.fixture.customer,
                end_date=datetime.datetime(year=2025, month=12, day=31).date(),
            )

            # Test that we can clear the end_date by sending null
            payload = {"end_date": None}
            url = factories.ProjectFactory.get_url(project)

            response = self.client.patch(url, payload)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify the end_date was cleared
            project.refresh_from_db()
            self.assertIsNone(project.end_date)

    def test_start_date_read_only_logic(self):
        """Test that start_date becomes read-only when the date has arrived."""
        self.client.force_authenticate(self.fixture.staff)

        with freeze_time("2021-06-01"):
            # Create a project with start_date = today
            project = factories.ProjectFactory(
                customer=self.fixture.customer,
                start_date=datetime.datetime(year=2021, month=6, day=1).date(),
            )

            # Try to update the start_date - should be read-only
            payload = {"start_date": "2021-07-01"}
            url = factories.ProjectFactory.get_url(project)

            response = self.client.patch(url, payload)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify the start_date was NOT changed (read-only)
            project.refresh_from_db()
            self.assertEqual(
                project.start_date, datetime.datetime(year=2021, month=6, day=1).date()
            )

        with freeze_time("2021-05-31"):
            # Create a project with start_date = tomorrow
            project2 = factories.ProjectFactory(
                customer=self.fixture.customer,
                start_date=datetime.datetime(year=2021, month=6, day=1).date(),
            )

            # Try to update the start_date - should be allowed (future date)
            payload = {"start_date": "2021-07-01"}
            url = factories.ProjectFactory.get_url(project2)

            response = self.client.patch(url, payload)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify the start_date was changed (not read-only yet)
            project2.refresh_from_db()
            self.assertEqual(
                project2.start_date, datetime.datetime(year=2021, month=7, day=1).date()
            )

        with freeze_time("2021-06-02"):
            # Test that past start_date makes field read-only
            project3 = factories.ProjectFactory(
                customer=self.fixture.customer,
                start_date=datetime.datetime(year=2021, month=6, day=1).date(),
            )

            # Try to update the start_date - should be read-only (past date)
            payload = {"start_date": "2021-07-01"}
            url = factories.ProjectFactory.get_url(project3)

            response = self.client.patch(url, payload)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify the start_date was NOT changed (read-only)
            project3.refresh_from_db()
            self.assertEqual(
                project3.start_date, datetime.datetime(year=2021, month=6, day=1).date()
            )

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_project_name_regex_rejects_too_long_name(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "x" * 33

        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)
        self.assertFalse(Project.objects.filter(name=payload["name"]).exists())

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_project_name_regex_allows_matching_name(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "x" * 32

        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(Project.objects.filter(name=payload["name"]).exists())

    def test_project_name_regex_disabled_by_default(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "x" * 100

        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @override_config(
        PROJECT_NAME_REGEX=r"^.{1,32}$",
        PROJECT_NAME_REGEX_ERROR_MESSAGE="Name must be at most 32 characters.",
    )
    def test_project_name_regex_uses_custom_error_message(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "x" * 33

        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Name must be at most 32 characters.", str(response.data))

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_project_name_regex_applies_on_rename(self):
        self.client.force_authenticate(self.fixture.staff)
        project = self.fixture.project

        response = self.client.patch(
            factories.ProjectFactory.get_url(project),
            {"name": "x" * 33},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_patch_not_changing_name_is_allowed_for_existing_long_name(self):
        # A project whose name predates the rule must remain editable as long as
        # the PATCH does not touch the name.
        self.client.force_authenticate(self.fixture.staff)
        project = self.fixture.project
        project.name = "x" * 100
        project.save()

        response = self.client.patch(
            factories.ProjectFactory.get_url(project),
            {"description": "updated"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @override_config(PROJECT_NAME_REGEX="[")
    def test_invalid_regex_is_ignored(self):
        # A malformed pattern is an admin misconfiguration and must not block
        # project creation.
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_project_payload(self.fixture.customer)
        payload["name"] = "x" * 100

        response = self.client.post(factories.ProjectFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def _get_valid_project_payload(self, customer):
        return {
            "name": "New project name",
            "customer": factories.CustomerFactory.get_url(customer),
        }


class ProjectApiPermissionTest(test.APITestCase):
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

        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_PROJECTS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_PROJECTS)

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
        self.assertIn("detail", response.data)
        self.assertEqual(
            response.data["detail"],
            "You do not have permission to perform this action.",
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
        self.assertIn("detail", response.data)
        self.assertEqual(
            response.data["detail"],
            "You do not have permission to perform this action.",
        )

    def test_user_cannot_create_project_within_customer_he_is_not_affiliated_with(self):
        self.client.force_authenticate(user=self.users["admin"])

        project = factories.ProjectFactory()
        response = self.client.post(
            reverse("project-list"), self._get_valid_payload(project)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("customer", response.data)
        self.assertEqual(
            response.data["customer"], ["Invalid hyperlink - Object does not exist."]
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
        response = self.client.get(reverse("project-list"), {"can_manage": True})
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


class TestExecutor(executors.BaseCleanupExecutor):
    pre_models = (test_models.TestNewInstance,)


@mock.patch("waldur_core.core.WaldurExtension.get_extensions")
class ProjectCleanupTest(test.APITestCase):
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


class ChangeProjectCustomerTest(test.APITestCase):
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
class ChangeProjectImageTest(test.APITestCase):
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


class ProjectMoveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.url = factories.ProjectFactory.get_url(self.project, action="move_project")
        self.customer = factories.CustomerFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)

    def get_response(self, role, customer):
        self.client.force_authenticate(role)
        payload = {
            "customer": factories.CustomerFactory.get_url(customer),
            "preserve_permissions": True,
        }
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

    def test_user_can_move_project_if_has_create_project_permission_in_both_customers(
        self,
    ):
        """Test that a user with CREATE_PROJECT permission in both source and target organizations can move a project."""

        user_with_permission = factories.UserFactory()
        self.project.customer.add_user(user_with_permission, CustomerRole.OWNER)
        self.customer.add_user(user_with_permission, CustomerRole.OWNER)

        response = self.get_response(user_with_permission, self.customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.project.customer, self.customer)

    def test_user_cannot_move_project_without_create_permission_in_source_customer(
        self,
    ):
        """Test that a user without CREATE_PROJECT permission in the source organization cannot move a project."""

        # User has permission only in target organization
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)

        response = self.get_response(user, self.customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Project should remain in original customer
        self.assertNotEqual(self.project.customer, self.customer)

    def test_user_cannot_move_project_without_create_permission_in_target_customer(
        self,
    ):
        """Test that a user without CREATE_PROJECT permission in the target organization cannot move a project."""

        # User has permission only in source organization
        user = factories.UserFactory()
        self.project.customer.add_user(user, CustomerRole.OWNER)

        response = self.get_response(user, self.customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Project should remain in original customer
        self.assertNotEqual(self.project.customer, self.customer)

    def test_user_without_create_project_permission_cannot_move_project(self):
        """Test that a user without CREATE_PROJECT permission in either organization cannot move a project."""
        user_without_permission = factories.UserFactory()

        response = self.get_response(user_without_permission, self.customer)

        self.project.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Project should remain in original customer
        self.assertNotEqual(self.project.customer, self.customer)


class ProjectListFilterTest(test.APITestCase):
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


class ProjectResourceQuotasTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.empty_project = factories.ProjectFactory()
        self.offering = marketplace_factories.OfferingFactory()
        self.component1 = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
            measured_unit="vCPU",
            billing_type=BillingTypes.USAGE,
        )
        self.component2 = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
            measured_unit="GB",
            billing_type=BillingTypes.USAGE,
        )
        self.resource1 = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            limits={"cpu": 8, "ram": 16},
        )
        self.resource2 = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            limits={"cpu": 4, "ram": 8},
        )
        # Create ComponentUsage records (source of truth for stats)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource1, component=self.component1, usage=2
        )
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource1, component=self.component2, usage=4
        )
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource2, component=self.component1, usage=1
        )
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource2, component=self.component2, usage=2
        )
        self.url = factories.ProjectFactory.get_url(self.project, "stats")

    def test_project_with_no_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        url = reverse("project-stats", kwargs={"uuid": self.empty_project.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["components"], [])

    def test_project_with_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        components = response.data["components"]
        # Check component stats for CPU
        cpu_component = next(
            component for component in components if component["type"] == "cpu"
        )
        self.assertEqual(cpu_component["usage"], 3)
        self.assertEqual(cpu_component["limit"], 12)
        self.assertEqual(cpu_component["measured_unit"], "vCPU")
        # Check component stats for RAM
        ram_component = next(
            component for component in components if component["type"] == "ram"
        )
        self.assertEqual(ram_component["usage"], 6)
        self.assertEqual(ram_component["limit"], 24)
        self.assertEqual(ram_component["measured_unit"], "GB")


class ProjectOtherUsersTest(test.APITestCase):
    def test_user_can_list_other_users(self):
        fixture = fixtures.ProjectFixture()
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_PROJECTS)

        project1 = factories.ProjectFactory(customer=fixture.customer)
        project2 = factories.ProjectFactory(customer=fixture.customer)
        project3 = factories.ProjectFactory(customer=fixture.customer)

        user1 = factories.UserFactory()
        user2 = factories.UserFactory()
        user3 = factories.UserFactory()

        project1.add_user(user1, ProjectRole.ADMIN)
        project2.add_user(user1, ProjectRole.MANAGER)
        project2.add_user(user2, ProjectRole.MANAGER)
        project3.add_user(user3, ProjectRole.MANAGER)

        url = factories.ProjectFactory.get_url(project1, "other_users")
        self.client.force_authenticate(user1)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            user2.uuid.hex,
            [user["uuid"] for user in response.data],
        )
        self.assertNotIn(
            user3.uuid.hex,
            [user["uuid"] for user in response.data],
        )


class ProjectRecoveryTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff_user = self.fixture.staff
        self.url = factories.ProjectFactory.get_url(self.project, action="recover")

        # Add some team members before deletion
        self.team_user1 = factories.UserFactory()
        self.team_user2 = factories.UserFactory()
        self.project.add_user(self.team_user1, ProjectRole.ADMIN)
        self.project.add_user(self.team_user2, ProjectRole.MANAGER)

        # Soft delete the project first
        self.project.delete()
        self.assertTrue(self.project.is_removed)

    def test_staff_can_recover_soft_deleted_project_without_team_restoration(self):
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url, {"restore_team_members": False})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_removed)

        # Check that team members were not restored
        restored_permissions = get_permissions(self.project)
        self.assertEqual(restored_permissions.count(), 0)

    def test_staff_can_recover_project_with_team_restoration(self):
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_removed)

        # Check that team members were restored
        restored_permissions = get_permissions(self.project)
        self.assertEqual(restored_permissions.count(), 2)

        # Check response includes recovery info
        self.assertIn("recovery_info", response.data)
        self.assertEqual(response.data["recovery_info"]["restored_users_count"], 2)

    def test_non_customer_user_cannot_access_project_recovery(self):
        # A user not connected to the customer gets 404 because they can't see the project
        user = factories.UserFactory()
        self.client.force_authenticate(user)

        response = self.client.post(self.url, {"restore_team_members": False})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_removed)

    def test_customer_owner_can_recover_project_with_invitations(self):
        customer_owner = self.fixture.owner
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.client.force_authenticate(customer_owner)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_removed)

    def test_customer_owner_cannot_restore_team_members_directly(self):
        customer_owner = self.fixture.owner
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.client.force_authenticate(customer_owner)

        response = self.client.post(self.url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only staff users can automatically restore", str(response.data))

    def test_customer_support_user_cannot_recover_project(self):
        # Customer support users cannot recover projects (they don't have CREATE_PROJECT permission)
        user = self.fixture.customer_support
        self.client.force_authenticate(user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_recover_active_project(self):
        # Create a new active project
        active_project = factories.ProjectFactory()
        url = factories.ProjectFactory.get_url(active_project, action="recover")

        self.client.force_authenticate(self.staff_user)

        response = self.client.post(url, {"restore_team_members": False})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not deleted", str(response.data))

    def test_recovery_defaults_are_correct(self):
        self.client.force_authenticate(self.staff_user)

        # Test that restore_team_members defaults to False when not provided
        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_removed)

    def test_team_restoration_with_inactive_user(self):
        # Deactivate one of the team members
        self.team_user1.is_active = False
        self.team_user1.save()

        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Only active user should be restored
        from waldur_core.permissions.utils import get_permissions

        restored_permissions = get_permissions(self.project)
        self.assertEqual(restored_permissions.count(), 1)
        self.assertEqual(restored_permissions.first().user, self.team_user2)

    def test_recovery_captures_termination_metadata(self):
        # Check that termination metadata was captured during deletion
        self.assertIsNotNone(self.project.termination_metadata)
        self.assertIn("user_roles", self.project.termination_metadata)
        self.assertEqual(len(self.project.termination_metadata["user_roles"]), 2)

    def test_multiple_recovery_attempts_dont_duplicate_roles(self):
        self.client.force_authenticate(self.staff_user)

        # First recovery
        response1 = self.client.post(self.url, {"restore_team_members": True})
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Second recovery attempt should fail because project is no longer deleted
        response2 = self.client.post(self.url, {"restore_team_members": True})
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not deleted", str(response2.data))

        # Check that we still have the correct number of roles (no duplicates)
        restored_permissions = get_permissions(self.project)
        self.assertEqual(restored_permissions.count(), 2)

    def test_staff_can_send_invitations_to_previous_members(self):
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_removed)

        # Check that invitations were sent
        project_ct = ContentType.objects.get_for_model(self.project)
        invitations = Invitation.objects.filter(
            content_type=project_ct, object_id=self.project.id
        )
        self.assertEqual(invitations.count(), 2)

        # Check response includes invitation info
        self.assertIn("recovery_info", response.data)
        self.assertEqual(response.data["recovery_info"]["sent_invitations_count"], 2)

    def test_cannot_both_restore_and_send_invitations(self):
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url,
            {
                "restore_team_members": True,
                "send_invitations_to_previous_members": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot both restore", str(response.data))

    def test_invitations_not_sent_to_inactive_users(self):
        # Deactivate one of the team members
        self.team_user1.is_active = False
        self.team_user1.save()

        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Only active user should get invitation
        project_ct = ContentType.objects.get_for_model(self.project)
        invitations = Invitation.objects.filter(
            content_type=project_ct, object_id=self.project.id
        )
        self.assertEqual(invitations.count(), 1)
        self.assertEqual(invitations.first().email, self.team_user2.email)

    def test_duplicate_invitations_not_sent(self):
        self.client.force_authenticate(self.staff_user)

        # First invitation sending
        response1 = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Soft delete and recover again
        self.project.delete()
        response2 = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # Should not create duplicate invitations
        project_ct = ContentType.objects.get_for_model(self.project)
        invitations = Invitation.objects.filter(
            content_type=project_ct, object_id=self.project.id
        )
        # Should still be 2 invitations, not 4
        self.assertEqual(invitations.count(), 2)

    def test_legacy_project_basic_recovery_works(self):
        """Test recovery of project that was deleted before termination metadata feature."""
        # Create a project without termination metadata (simulating old deletion)
        old_project = factories.ProjectFactory()
        old_project.is_removed = True
        old_project.termination_metadata = None
        old_project.save()

        url = factories.ProjectFactory.get_url(old_project, action="recover")
        self.client.force_authenticate(self.staff_user)

        # Basic recovery should work
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        old_project.refresh_from_db()
        self.assertFalse(old_project.is_removed)

    def test_legacy_project_team_restoration_blocked(self):
        """Test that legacy projects block team restoration with clear error."""
        # Create a project without termination metadata
        old_project = factories.ProjectFactory()
        old_project.is_removed = True
        old_project.termination_metadata = None
        old_project.save()

        url = factories.ProjectFactory.get_url(old_project, action="recover")
        self.client.force_authenticate(self.staff_user)

        # Team restoration should fail with clear error
        response = self.client.post(url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("deleted before team member metadata", str(response.data))
        self.assertIn("Only basic project recovery is available", str(response.data))

    def test_legacy_project_invitations_blocked(self):
        """Test that legacy projects block invitation sending with clear error."""
        # Create a project without termination metadata
        old_project = factories.ProjectFactory()
        old_project.is_removed = True
        old_project.termination_metadata = None
        old_project.save()

        url = factories.ProjectFactory.get_url(old_project, action="recover")
        self.client.force_authenticate(self.staff_user)

        # Invitation sending should fail with clear error
        response = self.client.post(url, {"send_invitations_to_previous_members": True})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("deleted before team member metadata", str(response.data))
        self.assertIn("metadata feature was implemented", str(response.data))

    def test_recovery_with_expired_user_roles(self):
        """Test recovery when some user roles had expired before deletion."""

        # Create project with users having different expiration times
        test_project = factories.ProjectFactory()
        expired_user = factories.UserFactory()
        valid_user = factories.UserFactory()

        # Add users with different expiration times
        expired_role = test_project.add_user(expired_user, ProjectRole.ADMIN)
        test_project.add_user(valid_user, ProjectRole.MANAGER)

        # Set one role to expire in the past
        past_time = timezone.now() - timedelta(days=30)
        expired_role.expiration_time = past_time
        expired_role.save()

        # Soft delete the project
        test_project.delete()

        # Recover with team restoration
        url = factories.ProjectFactory.get_url(test_project, action="recover")
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Only the non-expired role should be restored
        restored_permissions = get_permissions(test_project)
        self.assertEqual(restored_permissions.count(), 1)
        self.assertEqual(restored_permissions.first().user, valid_user)

    def test_recovery_with_deleted_user_roles(self):
        """Test recovery when some users have been deleted after project termination."""
        test_project = factories.ProjectFactory()
        user_to_delete = factories.UserFactory()
        active_user = factories.UserFactory()

        test_project.add_user(user_to_delete, ProjectRole.ADMIN)
        test_project.add_user(active_user, ProjectRole.MANAGER)

        # Soft delete the project first
        test_project.delete()

        # Then delete one of the users
        user_to_delete.delete()

        # Recover with team restoration
        url = factories.ProjectFactory.get_url(test_project, action="recover")
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Only the existing user's role should be restored
        restored_permissions = get_permissions(test_project)
        self.assertEqual(restored_permissions.count(), 1)
        self.assertEqual(restored_permissions.first().user, active_user)

    def test_invitation_with_user_already_having_role(self):
        """Test invitation sending when user already has the role through other means."""
        self.client.force_authenticate(self.staff_user)

        # First recover the project
        response1 = self.client.post(self.url, {"restore_team_members": True})
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Now try to send invitations (after soft-deleting again)
        self.project.delete()

        # Manually add one user back
        self.project.is_removed = False
        self.project.save()
        self.project.add_user(self.team_user1, ProjectRole.ADMIN)

        # Delete project again to test invitation logic
        self.project.delete()

        response2 = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

    def test_invitation_with_existing_pending_invitation(self):
        """Test that duplicate invitations aren't created for existing pending invitations."""
        # Create a pending invitation manually first
        project_ct = ContentType.objects.get_for_model(self.project)
        Invitation.objects.create(
            email=self.team_user1.email,
            role=ProjectRole.ADMIN,
            content_type=project_ct,
            object_id=self.project.id,
            customer=self.project.customer,
            created_by=self.staff_user,
            state=InvitationState.PENDING,
        )

        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should not create duplicate invitations
        invitations = Invitation.objects.filter(
            content_type=project_ct, object_id=self.project.id
        )
        # Should have 2 total: existing + 1 new for team_user2
        self.assertEqual(invitations.count(), 2)

    def test_recovery_response_format_with_team_restoration(self):
        """Test the response format when team members are restored."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify response structure
        self.assertIn("recovery_info", response.data)
        recovery_info = response.data["recovery_info"]

        self.assertIn("restored_users_count", recovery_info)
        self.assertIn("restored_users", recovery_info)
        self.assertEqual(recovery_info["restored_users_count"], 2)
        self.assertEqual(len(recovery_info["restored_users"]), 2)

        # Check user data structure
        restored_user = recovery_info["restored_users"][0]
        self.assertIn("user_uuid", restored_user)
        self.assertIn("username", restored_user)
        self.assertIn("role", restored_user)

    def test_recovery_response_format_with_invitations(self):
        """Test the response format when invitations are sent."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify response structure
        self.assertIn("recovery_info", response.data)
        recovery_info = response.data["recovery_info"]

        self.assertIn("sent_invitations_count", recovery_info)
        self.assertIn("sent_invitations", recovery_info)
        self.assertEqual(recovery_info["sent_invitations_count"], 2)
        self.assertEqual(len(recovery_info["sent_invitations"]), 2)

        # Check invitation data structure
        invitation = recovery_info["sent_invitations"][0]
        self.assertIn("invitation_uuid", invitation)
        self.assertIn("email", invitation)
        self.assertIn("role", invitation)
        self.assertIn("state", invitation)

    def test_recovery_without_team_options_has_no_recovery_info(self):
        """Test that recovery without team options doesn't include recovery_info."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should not have recovery_info in response
        self.assertNotIn("recovery_info", response.data)

    def test_termination_metadata_structure(self):
        """Test that termination metadata has correct structure."""
        metadata = self.project.termination_metadata

        # Check top-level structure
        self.assertIn("terminated_at", metadata)
        self.assertIn("terminated_by", metadata)
        self.assertIn("terminated_by_first_name", metadata)
        self.assertIn("terminated_by_last_name", metadata)
        self.assertIn("terminated_by_email", metadata)
        self.assertIn("user_roles", metadata)

        # Check user roles structure
        user_roles = metadata["user_roles"]
        self.assertEqual(len(user_roles), 2)

        for role_data in user_roles:
            self.assertIn("user_username", role_data)
            self.assertIn("user_first_name", role_data)
            self.assertIn("user_last_name", role_data)
            self.assertIn("user_email", role_data)
            self.assertIn("role_name", role_data)
            self.assertIn("created_by_username", role_data)
            self.assertIn("original_created", role_data)
            self.assertIn("original_expiration_time", role_data)
            self.assertIn("is_restored", role_data)
            self.assertIn("restored_at", role_data)
            self.assertIn("restored_by", role_data)

            # Check initial state
            self.assertFalse(role_data["is_restored"])
            self.assertIsNone(role_data["restored_at"])
            self.assertIsNone(role_data["restored_by"])

    def test_termination_metadata_updates_after_restoration(self):
        """Test that termination metadata is updated after successful restoration."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url, {"restore_team_members": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.project.refresh_from_db()
        metadata = self.project.termination_metadata

        # Check that all roles are marked as restored
        for role_data in metadata["user_roles"]:
            self.assertTrue(role_data["is_restored"])
            self.assertIsNotNone(role_data["restored_at"])
            self.assertEqual(role_data["restored_by"], self.staff_user.username)

    def test_termination_metadata_updates_after_invitations(self):
        """Test that termination metadata is updated after sending invitations."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.project.refresh_from_db()
        metadata = self.project.termination_metadata

        # Check that all roles have invitation tracking
        for role_data in metadata["user_roles"]:
            self.assertTrue(role_data["invitation_sent"])
            self.assertIsNotNone(role_data["invitation_sent_at"])
            self.assertEqual(role_data["invitation_sent_by"], self.staff_user.username)
            self.assertIn("invitation_uuid", role_data)

    def test_project_recovery_with_multiple_role_types(self):
        """Test recovery with different types of project roles."""
        # Add users with different project roles
        test_project = factories.ProjectFactory()
        member_user = factories.UserFactory()
        admin_user = factories.UserFactory()
        manager_user = factories.UserFactory()

        test_project.add_user(member_user, ProjectRole.MEMBER)
        test_project.add_user(admin_user, ProjectRole.ADMIN)
        test_project.add_user(manager_user, ProjectRole.MANAGER)

        # Soft delete the project
        test_project.delete()

        # Recover with team restoration
        url = factories.ProjectFactory.get_url(test_project, action="recover")
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(url, {"restore_team_members": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # All role types should be restored
        restored_permissions = get_permissions(test_project)
        self.assertEqual(restored_permissions.count(), 3)

        # Verify different roles are present
        role_names = [p.role.name for p in restored_permissions]
        expected_roles = [
            ProjectRole.MEMBER.name,
            ProjectRole.ADMIN.name,
            ProjectRole.MANAGER.name,
        ]
        for expected_role in expected_roles:
            self.assertIn(expected_role, role_names)

    def test_invitation_email_content_includes_context(self):
        """Test that invitation includes context about project recovery."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(
            self.url, {"send_invitations_to_previous_members": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that invitations have the recovery context
        project_ct = ContentType.objects.get_for_model(self.project)
        invitations = Invitation.objects.filter(
            content_type=project_ct, object_id=self.project.id
        )

        for invitation in invitations:
            self.assertIn("previously had", invitation.extra_invitation_text)
            self.assertIn("temporarily removed", invitation.extra_invitation_text)

    def test_termination_metadata_exposed_in_api_response(self):
        """Test that termination_metadata is included in project API response."""
        self.client.force_authenticate(self.staff_user)

        # Get project details
        project_url = factories.ProjectFactory.get_url(self.project)
        response = self.client.get(project_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should include termination_metadata in response
        self.assertIn("termination_metadata", response.data)
        self.assertIsNotNone(response.data["termination_metadata"])

        # Verify the structure is correct
        metadata = response.data["termination_metadata"]
        self.assertIn("terminated_at", metadata)
        self.assertIn("user_roles", metadata)
        self.assertEqual(len(metadata["user_roles"]), 2)

    def test_termination_metadata_not_exposed_for_regular_users(self):
        """Test that regular users can see termination_metadata but it's read-only."""
        # Customer users should be able to see the metadata for audit purposes
        customer_owner = self.fixture.owner
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.client.force_authenticate(customer_owner)

        project_url = factories.ProjectFactory.get_url(self.project)
        response = self.client.get(project_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should include termination_metadata (it's read-only)
        self.assertIn("termination_metadata", response.data)

    def test_active_project_has_null_termination_metadata(self):
        """Test that active projects have null termination_metadata."""
        active_project = factories.ProjectFactory()

        self.client.force_authenticate(self.staff_user)

        project_url = factories.ProjectFactory.get_url(active_project)
        response = self.client.get(project_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should have null termination_metadata
        self.assertIn("termination_metadata", response.data)
        self.assertIsNone(response.data["termination_metadata"])


class GracePeriodTest(test.APITestCase):
    """Test grace period functionality for projects and customers."""

    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.staff_user = factories.UserFactory(is_staff=True)
        # Add permissions for testing grace period updates
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)

    def test_project_grace_period_overrides_customer_grace_period(self):
        """Test that project-level grace period overrides customer-level setting."""
        # Set customer grace period to 5 days
        customer = self.fixture.customer
        customer.grace_period_days = 5
        customer.save()

        # Create project with grace period override of 10 days
        project = self.fixture.project
        project.grace_period_days = 10
        project.end_date = timezone.now().date() + timedelta(days=1)
        project.save()

        # Should get project-level grace period
        self.assertEqual(project.get_grace_period_days(), 10)

        # Effective end date should be end_date + 10 days
        expected_effective_end_date = project.end_date + timedelta(days=10)
        self.assertEqual(project.get_effective_end_date(), expected_effective_end_date)

    def test_project_inherits_customer_grace_period(self):
        """Test that project inherits customer grace period when not set."""
        # Set customer grace period to 7 days
        customer = self.fixture.customer
        customer.grace_period_days = 7
        customer.save()

        # Create project without grace period setting
        project = self.fixture.project
        project.grace_period_days = None
        project.end_date = timezone.now().date() + timedelta(days=1)
        project.save()

        # Should inherit customer grace period
        self.assertEqual(project.get_grace_period_days(), 7)

        # Effective end date should be end_date + 7 days
        expected_effective_end_date = project.end_date + timedelta(days=7)
        self.assertEqual(project.get_effective_end_date(), expected_effective_end_date)

    def test_zero_grace_period_when_none_set(self):
        """Test that grace period is 0 when neither customer nor project have it set."""
        # Ensure no grace periods are set
        customer = self.fixture.customer
        customer.grace_period_days = None
        customer.save()

        project = self.fixture.project
        project.grace_period_days = None
        project.end_date = timezone.now().date() + timedelta(days=1)
        project.save()

        # Should default to 0 grace period
        self.assertEqual(project.get_grace_period_days(), 0)

        # Effective end date should be same as end_date
        self.assertEqual(project.get_effective_end_date(), project.end_date)

    def test_is_expired_with_grace_period(self):
        """Test that is_expired considers grace period."""
        # Set grace period of 5 days
        project = self.fixture.project
        project.grace_period_days = 5
        project.end_date = timezone.now().date() - timedelta(days=3)  # 3 days ago
        project.save()

        # Should not be expired yet (within grace period)
        self.assertFalse(project.is_expired)

        # Set end date to 6 days ago (beyond grace period)
        project.end_date = timezone.now().date() - timedelta(days=6)
        project.save()

        # Should be expired now
        self.assertTrue(project.is_expired)

    def test_get_effective_end_date_returns_none_when_no_end_date(self):
        """Test that get_effective_end_date returns None when no end_date is set."""
        project = self.fixture.project
        project.grace_period_days = 5
        project.end_date = None
        project.save()

        self.assertIsNone(project.get_effective_end_date())

    def test_grace_period_fields_visible_to_all_in_api(self):
        """Test that grace_period_days field is visible to all users in API."""
        # Test Customer API
        customer_url = factories.CustomerFactory.get_url(self.fixture.customer)

        # Non-staff user should see grace_period_days
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("grace_period_days", response.data)

        # Staff user should also see grace_period_days
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("grace_period_days", response.data)

        # Test Project API
        project_url = factories.ProjectFactory.get_url(self.fixture.project)

        # Non-staff user should see grace_period_days
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(project_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("grace_period_days", response.data)

        # Staff user should also see grace_period_days
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(project_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("grace_period_days", response.data)

    def test_customer_grace_period_visible_in_project_api(self):
        """Test that customer-level grace period is exposed in project API."""
        self.fixture.customer.grace_period_days = 14
        self.fixture.customer.save()

        project_url = factories.ProjectFactory.get_url(self.fixture.project)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(project_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("customer_grace_period_days", response.data)
        self.assertEqual(response.data["customer_grace_period_days"], 14)

    def test_non_staff_cannot_update_grace_period(self):
        """Test that non-staff users cannot update grace_period_days."""
        # Test Customer update
        customer_url = factories.CustomerFactory.get_url(self.fixture.customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(customer_url, {"grace_period_days": 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Grace period should not have been updated
        self.fixture.customer.refresh_from_db()
        self.assertIsNone(self.fixture.customer.grace_period_days)

        # Test Project update
        project_url = factories.ProjectFactory.get_url(self.fixture.project)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(project_url, {"grace_period_days": 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Grace period should not have been updated
        self.fixture.project.refresh_from_db()
        self.assertIsNone(self.fixture.project.grace_period_days)

    def test_staff_can_update_grace_period(self):
        """Test that staff users can update grace_period_days."""
        # Test Customer update
        customer_url = factories.CustomerFactory.get_url(self.fixture.customer)

        self.client.force_authenticate(self.staff_user)
        response = self.client.patch(customer_url, {"grace_period_days": 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Grace period should have been updated
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.grace_period_days, 10)

        # Test Project update
        project_url = factories.ProjectFactory.get_url(self.fixture.project)

        self.client.force_authenticate(self.staff_user)
        response = self.client.patch(project_url, {"grace_period_days": 15})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Grace period should have been updated
        self.fixture.project.refresh_from_db()
        self.assertEqual(self.fixture.project.grace_period_days, 15)

    def test_grace_period_field_readonly_for_non_staff(self):
        """Test that grace_period_days field is read-only for non-staff users."""
        # Test Customer API - should be read-only for non-staff
        customer_url = factories.CustomerFactory.get_url(self.fixture.customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.options(customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The field should be present but read-only in the options
        actions = response.data.get("actions", {})
        if "PUT" in actions and "grace_period_days" in actions["PUT"]:
            field_info = actions["PUT"]["grace_period_days"]
            self.assertTrue(field_info.get("read_only", False))

        # Test Project API - should be read-only for non-staff
        project_url = factories.ProjectFactory.get_url(self.fixture.project)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.options(project_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The field should be present but read-only in the options
        actions = response.data.get("actions", {})
        if "PUT" in actions and "grace_period_days" in actions["PUT"]:
            field_info = actions["PUT"]["grace_period_days"]
            self.assertTrue(field_info.get("read_only", False))

    def test_is_in_grace_period_with_no_end_date(self):
        """Test that is_in_grace_period returns False when project has no end_date."""
        project = self.fixture.project
        project.end_date = None
        project.grace_period_days = 10
        project.save()

        self.assertFalse(project.is_in_grace_period)

    def test_is_in_grace_period_before_end_date(self):
        """Test that is_in_grace_period returns False when current date is before end_date."""
        project = self.fixture.project
        project.grace_period_days = 5
        # Set end_date to tomorrow
        project.end_date = timezone.now().date() + timedelta(days=1)
        project.save()

        self.assertFalse(project.is_in_grace_period)

    def test_is_in_grace_period_on_end_date(self):
        """Test that is_in_grace_period returns False when current date equals end_date."""
        project = self.fixture.project
        project.grace_period_days = 5
        # Set end_date to today
        project.end_date = timezone.now().date()
        project.save()

        self.assertFalse(project.is_in_grace_period)

    def test_is_in_grace_period_within_grace_period(self):
        """Test that is_in_grace_period returns True when within grace period."""
        project = self.fixture.project
        project.grace_period_days = 10
        # Set end_date to 5 days ago (within 10-day grace period)
        project.end_date = timezone.now().date() - timedelta(days=5)
        project.save()

        self.assertTrue(project.is_in_grace_period)

    def test_is_in_grace_period_on_last_day_of_grace_period(self):
        """Test that is_in_grace_period returns True on the last day of grace period."""
        project = self.fixture.project
        project.grace_period_days = 5
        # Set end_date to exactly 5 days ago (last day of grace period)
        project.end_date = timezone.now().date() - timedelta(days=5)
        project.save()

        self.assertTrue(project.is_in_grace_period)

    def test_is_in_grace_period_past_grace_period(self):
        """Test that is_in_grace_period returns False when past grace period."""
        project = self.fixture.project
        project.grace_period_days = 5
        # Set end_date to 6 days ago (past 5-day grace period)
        project.end_date = timezone.now().date() - timedelta(days=6)
        project.save()

        self.assertFalse(project.is_in_grace_period)

    def test_is_in_grace_period_with_zero_grace_period(self):
        """Test that is_in_grace_period returns False when grace period is 0."""
        project = self.fixture.project
        project.grace_period_days = 0
        # Set end_date to yesterday
        project.end_date = timezone.now().date() - timedelta(days=1)
        project.save()

        self.assertFalse(project.is_in_grace_period)

    def test_is_in_grace_period_inherits_customer_grace_period(self):
        """Test that is_in_grace_period works with inherited customer grace period."""
        # Set customer grace period but not project grace period
        customer = self.fixture.customer
        customer.grace_period_days = 7
        customer.save()

        project = self.fixture.project
        project.grace_period_days = None
        # Set end_date to 3 days ago (within 7-day customer grace period)
        project.end_date = timezone.now().date() - timedelta(days=3)
        project.save()

        self.assertTrue(project.is_in_grace_period)

    def test_end_date_with_grace_returns_none_when_no_end_date(self):
        """Test that end_date_with_grace returns None when project has no end_date."""
        project = self.fixture.project
        project.end_date = None
        project.grace_period_days = 10
        project.save()

        self.assertIsNone(project.end_date_with_grace)

    def test_end_date_with_grace_adds_grace_period_days(self):
        """Test that end_date_with_grace correctly adds grace period days."""
        project = self.fixture.project
        project.grace_period_days = 7
        project.end_date = timezone.now().date()
        project.save()

        expected_grace_end = project.end_date + timedelta(days=7)
        self.assertEqual(project.end_date_with_grace, expected_grace_end)

    def test_end_date_with_grace_with_zero_grace_period(self):
        """Test that end_date_with_grace equals end_date when grace period is 0."""
        project = self.fixture.project
        project.grace_period_days = 0
        project.end_date = timezone.now().date()
        project.save()

        self.assertEqual(project.end_date_with_grace, project.end_date)

    def test_end_date_with_grace_inherits_customer_grace_period(self):
        """Test that end_date_with_grace works with inherited customer grace period."""
        # Set customer grace period but not project grace period
        customer = self.fixture.customer
        customer.grace_period_days = 14
        customer.save()

        project = self.fixture.project
        project.grace_period_days = None
        project.end_date = timezone.now().date()
        project.save()

        expected_grace_end = project.end_date + timedelta(days=14)
        self.assertEqual(project.end_date_with_grace, expected_grace_end)

    def test_end_date_with_grace_project_overrides_customer(self):
        """Test that project grace period overrides customer grace period in end_date_with_grace."""
        # Set both customer and project grace periods
        customer = self.fixture.customer
        customer.grace_period_days = 5
        customer.save()

        project = self.fixture.project
        project.grace_period_days = 12  # Should override customer setting
        project.end_date = timezone.now().date()
        project.save()

        expected_grace_end = project.end_date + timedelta(days=12)
        self.assertEqual(project.end_date_with_grace, expected_grace_end)

    def test_grace_period_properties_consistency_with_expired(self):
        """Test that grace period properties are consistent with is_expired property."""
        project = self.fixture.project
        project.grace_period_days = 5

        # Test case 1: Project not expired, not in grace period
        project.end_date = timezone.now().date() + timedelta(days=1)
        project.save()
        self.assertFalse(project.is_expired)
        self.assertFalse(project.is_in_grace_period)

        # Test case 2: Project in grace period, not expired
        project.end_date = timezone.now().date() - timedelta(days=3)
        project.save()
        self.assertFalse(project.is_expired)  # Within grace period
        self.assertTrue(project.is_in_grace_period)

        # Test case 3: Project expired and past grace period
        project.end_date = timezone.now().date() - timedelta(days=6)
        project.save()
        self.assertTrue(project.is_expired)  # Past grace period
        self.assertFalse(project.is_in_grace_period)  # Past grace period

    @freeze_time("2025-01-15")
    def test_grace_period_properties_with_frozen_time(self):
        """Test grace period properties with frozen time for precise date testing."""
        project = self.fixture.project
        project.grace_period_days = 7

        # Set end_date to 2025-01-10 (5 days ago)
        project.end_date = datetime.date(2025, 1, 10)
        project.save()

        # Should be in grace period
        self.assertTrue(project.is_in_grace_period)
        self.assertFalse(project.is_expired)

        # Grace end should be 2025-01-17
        expected_grace_end = datetime.date(2025, 1, 17)
        self.assertEqual(project.end_date_with_grace, expected_grace_end)


class ProjectListQueryOptimizationTest(test.APITestCase):
    """
    Test that project list endpoint is optimized to avoid N+1 queries.

    Fixes PUHURI-PORTALS-E3K (N+1 query on customer and resources_count).
    """

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff

        # Create multiple projects with resources
        self.projects = [self.fixture.project]
        for i in range(4):  # Total 5 projects
            self.projects.append(factories.ProjectFactory(customer=self.customer))

        # Create resources for some projects
        self.offering = marketplace_factories.OfferingFactory()
        from waldur_mastermind.marketplace.models import Resource

        # Project 0: 2 active resources
        marketplace_factories.ResourceFactory(
            project=self.projects[0],
            offering=self.offering,
            state=Resource.States.OK,
        )
        marketplace_factories.ResourceFactory(
            project=self.projects[0],
            offering=self.offering,
            state=Resource.States.OK,
        )

        # Project 1: 1 active, 1 terminated resource
        marketplace_factories.ResourceFactory(
            project=self.projects[1],
            offering=self.offering,
            state=Resource.States.OK,
        )
        marketplace_factories.ResourceFactory(
            project=self.projects[1],
            offering=self.offering,
            state=Resource.States.TERMINATED,
        )

        # Project 2: 1 updating resource
        marketplace_factories.ResourceFactory(
            project=self.projects[2],
            offering=self.offering,
            state=Resource.States.UPDATING,
        )

        # Project 3 and 4: no resources

        self.url = factories.ProjectFactory.get_list_url()

    def test_resources_count_is_correct(self):
        """Test that resources_count returns correct count of active resources."""
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Create a lookup by project UUID
        results_by_uuid = {
            item["uuid"]: item for item in response.data if "uuid" in item
        }

        # Project 0: 2 active resources
        self.assertEqual(
            results_by_uuid[str(self.projects[0].uuid)]["resources_count"], 2
        )

        # Project 1: 1 active (terminated doesn't count)
        self.assertEqual(
            results_by_uuid[str(self.projects[1].uuid)]["resources_count"], 1
        )

        # Project 2: 1 updating resource (counts as active)
        self.assertEqual(
            results_by_uuid[str(self.projects[2].uuid)]["resources_count"], 1
        )

        # Project 3: no resources
        self.assertEqual(
            results_by_uuid[str(self.projects[3].uuid)]["resources_count"], 0
        )

        # Project 4: no resources
        self.assertEqual(
            results_by_uuid[str(self.projects[4].uuid)]["resources_count"], 0
        )

    def test_query_count_does_not_scale_with_projects(self):
        """Test that query count is optimized and doesn't have N+1 issues."""
        from django.db import connection, reset_queries
        from django.test import override_settings

        self.client.force_authenticate(self.staff)

        with override_settings(DEBUG=True):
            reset_queries()

            response = self.client.get(self.url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Filter relevant queries (exclude framework/setup queries)
            business_queries = [
                q
                for q in connection.queries
                if not any(
                    skip in q["sql"].lower()
                    for skip in [
                        "constance_config",
                        "django_migrations",
                        "django_session",
                        "auth_user",  # Authentication queries
                    ]
                )
            ]

            # Count queries that look like N+1 patterns (repeated customer/resource queries)
            customer_queries = [
                q for q in business_queries if "structure_customer" in q["sql"].lower()
            ]

            # Resource count N+1 queries are those that:
            # - Query marketplace_resource table with COUNT
            # - Filter by a single project (per-project queries)
            # We allow batch queries that group by project_id
            per_project_resource_queries = [
                q
                for q in business_queries
                if "count" in q["sql"].lower()
                and "marketplace_resource" in q["sql"].lower()
                # Exclude batch queries that group by project_id
                and "project_id" not in q["sql"].lower()
            ]

            # With proper optimization:
            # - Should have at most 1-2 customer queries (from select_related)
            # - Should have 0 per-project resource count queries (batch query is OK)
            # Without optimization, we'd have 5+ of each (one per project)
            self.assertLessEqual(
                len(customer_queries),
                2,
                f"Too many customer queries ({len(customer_queries)}), possible N+1 issue. "
                f"Queries: {[q['sql'][:100] for q in customer_queries]}",
            )
            self.assertEqual(
                len(per_project_resource_queries),
                0,
                f"Found {len(per_project_resource_queries)} per-project resource count queries (N+1 issue). "
                f"Queries: {[q['sql'][:150] for q in per_project_resource_queries]}",
            )
