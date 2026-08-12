import json
import logging
from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import utils as django_test
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import QuestionTypes
from waldur_core.checklist.tests.factories import ChecklistFactory, QuestionFactory
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models, serializers, utils
from waldur_mastermind.marketplace.enums import (
    OfferingUserRuntimeStates,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import OfferingUser
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OfferingUserFactory,
    ServiceProviderFactory,
)

from . import factories, fixtures


@ddt
class ListOfferingUsersTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()
        self.fixture.project.add_user(user, ProjectRole.ADMIN)
        OfferingUser.objects.create(offering=self.offering, user=user, username="user")
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        user2 = UserFactory()
        offering2 = factories.OfferingFactory(shared=True)
        self.fixture.project.add_user(user, ProjectRole.MANAGER)
        OfferingUser.objects.create(offering=offering2, user=user2, username="user2")

    def list_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.get(OfferingUserFactory.get_list_url())

    @data("owner", "admin", "manager")
    def test_authorized_user_can_list_offering_users(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 1)
        self.assertEqual("user", response.data[0]["username"])

    @data("staff", "global_support")
    def test_authorized_privileged_user_can_list_offering_users(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 2)

    @data(
        "user",
    )
    def test_unauthorized_user_can_not_list_offering_permission(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 0)

    def test_user_can_view_own_offering_user(self):
        sample_user = UserFactory()
        OfferingUser.objects.create(
            offering=self.offering, user=sample_user, username="user3"
        )
        models.UserOfferingConsent.objects.create(
            user=sample_user,
            offering=self.offering,
            version="1.0",
        )

        self.client.force_authenticate(sample_user)
        response = self.client.get(OfferingUserFactory.get_list_url())

        self.assertEqual(1, len(response.data))
        self.assertEqual("user3", response.data[0]["username"])

    @data("owner", "admin", "manager")
    def test_other_users_can_not_view_offering_users_when_offering_user_creation_disabled(
        self, user
    ):
        offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        sample_user = UserFactory()
        self.fixture.project.add_user(sample_user, ProjectRole.ADMIN)
        OfferingUser.objects.create(
            offering=offering, user=sample_user, username="remote-user"
        )

        response = self.list_permissions(user)
        self.assertNotIn("remote-user", [row["username"] for row in response.data])

    def test_user_can_filter_offering_users(self):
        offering_user1 = OfferingUser.objects.get(username="user")
        offering_user1.save()

        self.client.force_login(self.fixture.staff)

        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"provider_uuid": self.offering.customer.uuid.hex},
        )
        self.assertEqual(1, len(response.data))
        self.assertEqual("user", response.data[0]["username"])

    @data("user_first_name", "-user_first_name", "user_last_name", "-user_last_name")
    def test_user_can_order_offering_users_by_name(self, ordering):
        self.client.force_login(self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"o": ordering},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(2, len(response.data))

    def test_query_search_by_uid_and_gid(self):
        OfferingUser.objects.filter(username="user").update(
            backend_metadata={"uidnumber": 100042, "primarygroup": 200042}
        )
        self.client.force_login(self.fixture.staff)

        for value in ("100042", "200042", "0004"):  # exact uid, exact gid, partial
            response = self.client.get(
                OfferingUserFactory.get_list_url(), {"query": value}
            )
            self.assertEqual([u["username"] for u in response.data], ["user"], value)

    def test_user_can_filter_by_user_username(self):
        offering_user = OfferingUser.objects.get(username="user")
        user = offering_user.user
        user.username = "UserName1"
        user.save()

        self.client.force_login(self.fixture.staff)

        response = self.client.get(
            OfferingUserFactory.get_list_url(), {"user_username": "username1"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(offering_user.user.get_username(), user.username)


@ddt
class CreateOfferingUsersTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options["service_provider_can_create_offering_user"] = True
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_USER)

    def create_offering_user(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        offering_url = factories.OfferingFactory.get_url(self.offering)
        user_url = UserFactory.get_url(self.fixture.user)
        payload = {"offering": offering_url, "user": user_url}
        return self.client.post(OfferingUserFactory.get_list_url(), payload)

    @data("staff", "owner")
    def test_authorized_user_can_create_offering_user(self, user):
        response = self.create_offering_user(user)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)

    @data("staff", "owner")
    def test_offering_does_not_allow_to_create_user(self, user):
        self.offering.plugin_options["service_provider_can_create_offering_user"] = (
            False
        )
        self.offering.save()
        response = self.create_offering_user(user)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    @data("admin", "manager")
    def test_unauthorized_user_can_not_create_offering_user(self, user):
        response = self.create_offering_user(user)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_create_offering_user_with_uuid_fields(self):
        """Should succeed when only offering_uuid and user_uuid are provided."""
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {
            "offering_uuid": self.offering.uuid.hex,
            "user_uuid": self.fixture.user.uuid.hex,
            "username": "testuser",
        }
        response = self.client.post(OfferingUserFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_create_offering_user_with_both_url_and_uuid_fields(self):
        """Should fail when both URL and UUID fields are provided."""
        self.client.force_authenticate(user=self.fixture.owner)
        offering_url = factories.OfferingFactory.get_url(self.offering)
        user_url = UserFactory.get_url(self.fixture.user)
        payload = {
            "offering": offering_url,
            "user": user_url,
            "offering_uuid": self.offering.uuid.hex,
            "user_uuid": self.fixture.user.uuid.hex,
            "username": "testuser",
        }
        response = self.client.post(OfferingUserFactory.get_list_url(), payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_offering_user_with_missing_fields(self):
        """Should fail when neither URL nor UUID fields are provided."""
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {"username": "testuser"}
        response = self.client.post(OfferingUserFactory.get_list_url(), payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


@ddt
class ListUsersTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.admin
        self.fixture.manager
        self.fixture.member

        self.url = reverse("user-list")

    @data("service_manager", "offering_owner")
    def test_user_should_be_able_to_see_users_connected_with_public_resources(
        self, user
    ):
        self.fixture.offering.shared = True
        self.fixture.offering.save()

        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 4)

    @data("service_manager", "offering_owner")
    def test_user_should_not_be_able_to_see_users_connected_with_private_resources(
        self, user
    ):
        self.fixture.offering.shared = False
        self.fixture.offering.save()
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    @data("service_manager", "offering_owner", "user")
    def test_users_related_to_terminated_resources_are_not_exposed(self, user):
        self.fixture.offering.shared = True
        self.fixture.offering.save()

        self.fixture.resource.state = ResourceStates.TERMINATED
        self.fixture.resource.save()

        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)


@ddt
class OfferingUsersUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()

        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=user, username="user"
        )
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, offering_user, action=None):
        if action is None:
            return OfferingUserFactory.get_url(offering_user)
        return OfferingUserFactory.get_url(offering_user) + action + "/"

    def update_offering_user(self, user, offering_user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        url = self.get_url(offering_user)
        payload = {"username": "new_username"}
        return self.client.patch(url, payload)

    @data("staff", "owner")
    def test_authorized_user_can_update_offering_user(self, user):
        response = self.update_offering_user(user, self.offering_user)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.offering_user.refresh_from_db()
        self.assertEqual("new_username", self.offering_user.username)

    def test_username_update_is_logged(self):
        self.client.force_authenticate(user=self.fixture.staff)
        url = self.get_url(self.offering_user)

        with self.assertLogs(
            "waldur_mastermind.marketplace.views", level=logging.INFO
        ) as logs:
            response = self.client.patch(url, {"username": "new_username"})

        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.assertTrue(
            any("OfferingUser username update via API" in line for line in logs.output),
            logs.output,
        )

    @data("customer_support", "service_manager")
    def test_unauthorized_user_can_not_update_offering_user(self, user):
        response = self.update_offering_user(user, self.offering_user)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class OfferingUserPosixAttributesTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "homedir_prefix": "/home/hpc/",
        }
        self.offering.save()
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=UserFactory(),
            username="alice",
            backend_metadata={
                "uidnumber": 1000,
                "loginShell": "/bin/bash",
                "homeDir": "/home/hpc/alice",
            },
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, action=None):
        url = OfferingUserFactory.get_url(self.offering_user)
        return url if action is None else url + action + "/"

    # --- #1: home directory follows the username ---------------------------

    def test_home_directory_is_rederived_on_username_change(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.get_url(), {"username": "alice2"})
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.backend_metadata["homeDir"], "/home/hpc/alice2"
        )

    def test_overridden_home_directory_is_preserved_on_username_change(self):
        self.offering_user.backend_metadata["homeDir"] = "/custom/alice"
        self.offering_user.save()
        self.client.force_authenticate(self.fixture.staff)
        self.client.patch(self.get_url(), {"username": "alice2"})
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.backend_metadata["homeDir"], "/custom/alice"
        )

    # --- #2: per-user POSIX attribute overrides ----------------------------

    def test_posix_attributes_are_exposed_read_only(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.data["login_shell"], "/bin/bash")
        self.assertEqual(response.data["home_directory"], "/home/hpc/alice")

    @data("staff", "owner")
    def test_authorized_user_can_set_posix_attributes(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.get_url("set_posix_attributes"),
            {"login_shell": "/bin/zsh", "home_directory": "/data/alice"},
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["loginShell"], "/bin/zsh")
        self.assertEqual(self.offering_user.backend_metadata["homeDir"], "/data/alice")
        # The allocated uid is untouched.
        self.assertEqual(self.offering_user.backend_metadata["uidnumber"], 1000)

    def test_set_posix_attributes_accepts_partial_payload(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.get_url("set_posix_attributes"), {"login_shell": "/bin/sh"}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.backend_metadata["loginShell"], "/bin/sh")
        self.assertEqual(
            self.offering_user.backend_metadata["homeDir"], "/home/hpc/alice"
        )

    def test_set_posix_attributes_rejects_empty_payload(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url("set_posix_attributes"), {})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    @data("customer_support", "service_manager")
    def test_unauthorized_user_can_not_set_posix_attributes(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.get_url("set_posix_attributes"), {"login_shell": "/bin/zsh"}
        )
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class OfferingUserPosixGroupsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.manager, username="m"
        )
        group = models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={"gid": 8500}
        )
        group.projects.add(self.fixture.project)

    def get_url(self):
        return OfferingUserFactory.get_url(self.offering_user) + "posix_groups/"

    def test_lists_project_group_gids_for_member(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["gid"], 8500)
        self.assertEqual(row["project_uuid"], self.fixture.project.uuid.hex)
        self.assertEqual(row["customer_name"], self.fixture.customer.name)
        self.assertEqual(row["customer_uuid"], self.fixture.customer.uuid.hex)
        # Staff may open any project.
        self.assertTrue(row["project_accessible"])
        # The GID was seeded directly, so it is not tied to any pool.
        self.assertIsNone(row["pool_uuid"])

    def test_group_gid_shows_originating_pool_when_allocated(self):
        service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        pool = factories.PosixIdPoolFactory(
            service_provider=service_provider,
            min_uid=1000,
            max_uid=7999,
            next_uid=1000,
            min_gid=8000,
            max_gid=8999,
            next_gid=8000,
        )
        group = models.OfferingUserGroup.objects.get(offering=self.offering)
        models.PosixIdentity.objects.create(
            pool=pool, gid=8500, consumer=group, offering=self.offering
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        row = response.data[0]
        self.assertEqual(row["pool_uuid"], pool.uuid.hex)

    def test_project_not_accessible_for_unconnected_viewer(self):
        outsider = UserFactory()
        rows = utils.get_offering_user_posix_groups(self.offering_user, viewer=outsider)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["project_accessible"])
        # The organization is still reported.
        self.assertEqual(rows[0]["customer_name"], self.fixture.customer.name)

    def test_empty_when_group_has_no_gid(self):
        models.OfferingUserGroup.objects.all().update(backend_metadata={})
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.data, [])


@ddt
class OfferingUsersDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()

        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=user, username="user"
        )
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING_USER)

    def get_url(self, offering_user):
        return OfferingUserFactory.get_url(offering_user)

    def delete_offering_user(self, user, offering_user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        url = self.get_url(offering_user)
        return self.client.delete(url)

    @data("staff", "owner")
    def test_authorized_user_can_delete_offering_user(self, user):
        response = self.delete_offering_user(user, self.offering_user)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
        self.assertFalse(OfferingUser.objects.filter(pk=self.offering_user.pk).exists())

    @data("customer_support", "service_manager")
    def test_unauthorized_user_can_not_delete_offering_user(self, user):
        response = self.delete_offering_user(user, self.offering_user)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class OfferingUsersHandlerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_when_offering_user_is_created_audit_log_is_generated(self):
        OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.fixture.user,
            username="user",
        )
        self.assertTrue(
            Event.objects.filter(
                event_type="marketplace_offering_user_created"
            ).exists()
        )

    def test_when_offering_user_is_deleted_audit_log_is_generated(self):
        offering_user = OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.fixture.user,
            username="user",
        )
        offering_user.delete()
        self.assertTrue(
            Event.objects.filter(
                event_type="marketplace_offering_user_deleted"
            ).exists()
        )

    def test_when_offering_user_username_is_changed_audit_log_is_generated(self):
        self.client.force_authenticate(user=self.fixture.staff)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        self.fixture.offering.customer.add_user(self.fixture.staff, CustomerRole.OWNER)

        offering_user = OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.fixture.user,
            username="initial",
        )
        models.UserOfferingConsent.objects.create(
            user=self.fixture.user,
            offering=self.fixture.offering,
            version="1.0",
        )

        url = OfferingUserFactory.get_url(offering_user)

        response = self.client.patch(url, {"username": "new_username"})
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)

        event = (
            Event.objects.filter(event_type="marketplace_offering_user_updated")
            .order_by("-created")
            .first()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.context["old_username"], "initial")
        self.assertEqual(event.context["new_username"], "new_username")
        self.assertEqual(
            event.context["affected_user_uuid"], self.fixture.user.uuid.hex
        )
        self.assertIn("ip_address", event.context)

        response = self.client.patch(url, {"username": ""})
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)

        event = (
            Event.objects.filter(event_type="marketplace_offering_user_updated")
            .order_by("-created")
            .first()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.context["old_username"], "new_username")
        self.assertEqual(event.context["new_username"], "")
        self.assertEqual(
            event.context["affected_user_uuid"], self.fixture.user.uuid.hex
        )
        self.assertIn("ip_address", event.context)


@ddt
class OferingUserRestrictedUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()

        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=user, username="user"
        )
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, offering_user, action):
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        return url if action is None else url + action + "/"

    def update_restriction_status(self, offering_user):
        url = self.get_url(offering_user, "update_restricted")
        payload = {"is_restricted": True}
        response = self.client.post(url, payload)
        return response

    def test_user_can_not_update_offering_user_restriction(self):
        self.client.force_authenticate(user=self.fixture.user)
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.SUPPORT)
        response = self.update_restriction_status(self.offering_user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    @data("staff", "owner", "service_manager")
    def test_owner_manager_can_update_offering_user_restriction(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.update_restriction_status(self.offering_user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertTrue(self.offering_user.is_restricted)
        self.assertTrue(
            Event.objects.filter(
                event_type="marketplace_offering_user_restriction_updated"
            ).exists()
        )


@ddt
class OfferingUserStateTransitionTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()

        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=user, username="user"
        )
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, offering_user, action):
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        return url + action + "/"

    def test_new_offering_user_has_creation_requested_state(self):
        """Test that newly created OfferingUser has CREATION_REQUESTED state by default."""
        user = UserFactory()
        offering_user = OfferingUser.objects.create(offering=self.offering, user=user)
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_begin_creating_transition(self):
        """Test transition to CREATING state."""
        self.client.force_authenticate(user=self.fixture.owner)
        self.offering_user.state = OfferingUserStates.CREATION_REQUESTED
        self.offering_user.save()
        url = self.get_url(self.offering_user, "begin_creating")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.CREATING)

    def test_set_pending_additional_validation_transition(self):
        """Test transition to PENDING_ADDITIONAL_VALIDATION state with comment."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {"comment": "Additional documents required"}
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        self.assertEqual(
            self.offering_user.service_provider_comment, "Additional documents required"
        )

    def test_set_pending_account_linking_transition(self):
        """Test transition to PENDING_ACCOUNT_LINKING state with comment."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_account_linking")
        payload = {"comment": "Please link your existing account"}
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ACCOUNT_LINKING
        )
        self.assertEqual(
            self.offering_user.service_provider_comment,
            "Please link your existing account",
        )

    def test_set_validation_complete_transition(self):
        """Test transition from pending states to OK and comment clearing."""
        self.offering_user.state = OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        self.offering_user.service_provider_comment = "Some validation comment"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_validation_complete")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.OK)
        self.assertEqual(self.offering_user.service_provider_comment, "")

    def test_set_ok_transition(self):
        """Test transition to OK state."""
        self.client.force_authenticate(user=self.fixture.owner)
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()
        url = self.get_url(self.offering_user, "set_ok")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.OK)

    def test_state_transition_without_comment(self):
        """Test state transitions work without providing comment."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        self.assertEqual(self.offering_user.service_provider_comment, "")

    def test_unauthorized_user_cannot_change_state(self):
        """Test that unauthorized users cannot change offering user state."""
        unauthorized_user = UserFactory()
        self.client.force_authenticate(user=unauthorized_user)
        url = self.get_url(self.offering_user, "set_validation_complete")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

    def test_state_fields_in_serializer_output(self):
        """Test that state and comment fields are included in serializer output."""
        self.offering_user.state = OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        self.offering_user.service_provider_comment = "Test comment"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("state", response.data)
        self.assertIn("service_provider_comment", response.data)
        self.assertEqual(response.data["state"], "Pending additional validation")
        self.assertEqual(response.data["service_provider_comment"], "Test comment")

    def test_set_error_creating_transition(self):
        """Test transition to ERROR_CREATING state."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_error_creating")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.ERROR_CREATING)

    def test_set_error_deleting_transition(self):
        """Test transition to ERROR_DELETING state."""
        self.offering_user.state = OfferingUserStates.DELETING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_error_deleting")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.ERROR_DELETING)

    def test_recovery_from_error_creating_to_creating(self):
        """Test recovery from ERROR_CREATING to CREATING state."""
        self.offering_user.state = OfferingUserStates.ERROR_CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "begin_creating")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.CREATING)

    def test_recovery_from_error_creating_to_ok(self):
        """Test recovery from ERROR_CREATING directly to OK state."""
        self.offering_user.state = OfferingUserStates.ERROR_CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_ok")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.OK)

    def test_recovery_from_error_deleting_to_ok(self):
        """Test recovery from ERROR_DELETING to OK state when deletion fails but user should be restored."""
        self.offering_user.state = OfferingUserStates.ERROR_DELETING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_ok")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.OK)

    def test_legacy_set_error_defaults_to_error_creating(self):
        """Test that the legacy set_error method defaults to ERROR_CREATING state."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        # Use the legacy method directly
        self.offering_user.set_error()
        self.offering_user.save()

        self.assertEqual(self.offering_user.state, OfferingUserStates.ERROR_CREATING)

    def test_error_state_transitions_from_pending_states(self):
        """Test that error creating can be set from pending states."""
        for state in [
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
        ]:
            self.offering_user.state = state
            self.offering_user.save()

            # Should be able to transition to error creating
            self.offering_user.set_error_creating()
            self.offering_user.save()
            self.assertEqual(
                self.offering_user.state, OfferingUserStates.ERROR_CREATING
            )

            # Reset for next iteration
            self.offering_user.state = OfferingUserStates.CREATING
            self.offering_user.save()

    def test_service_provider_comment_url_transitions(self):
        """Test that comment URLs are properly handled in state transitions."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {
            "comment": "Additional documents required",
            "comment_url": "https://docs.example.com/validation",
        }
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        self.assertEqual(
            self.offering_user.service_provider_comment, "Additional documents required"
        )
        self.assertEqual(
            self.offering_user.service_provider_comment_url,
            "https://docs.example.com/validation",
        )

    def test_comment_url_cleared_on_validation_complete(self):
        """Test that comment URL is cleared when validation is complete."""
        self.offering_user.state = OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        self.offering_user.service_provider_comment = "Some validation comment"
        self.offering_user.service_provider_comment_url = "https://example.com/info"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_validation_complete")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.OK)
        self.assertEqual(self.offering_user.service_provider_comment, "")
        self.assertEqual(self.offering_user.service_provider_comment_url, "")

    def test_update_comments_action_by_service_provider(self):
        """Test service provider can update comments via update_comments action."""
        # Make the service provider user a manager
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

        service_provider = self.offering.customer
        service_provider_user = UserFactory()
        service_provider.add_user(service_provider_user, ServiceProviderRole.MANAGER)

        self.client.force_authenticate(user=service_provider_user)
        url = self.get_url(self.offering_user, "update_comments")
        payload = {
            "service_provider_comment": "Updated service comment",
            "service_provider_comment_url": "https://service.example.com/help",
        }
        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.service_provider_comment, "Updated service comment"
        )
        self.assertEqual(
            self.offering_user.service_provider_comment_url,
            "https://service.example.com/help",
        )

    def test_update_comments_action_unauthorized(self):
        """Test that unauthorized users cannot update service provider comments."""
        unauthorized_user = UserFactory()
        self.client.force_authenticate(user=unauthorized_user)
        url = self.get_url(self.offering_user, "update_comments")
        payload = {
            "service_provider_comment": "Unauthorized update",
            "service_provider_comment_url": "https://malicious.example.com",
        }
        response = self.client.patch(url, payload)

        # Unauthorized users get 404 since they can't even see the offering user object
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

    def test_html_sanitization_in_comments(self):
        """Test that HTML content in comments is properly sanitized."""
        self.client.force_authenticate(user=self.fixture.owner)

        # Test with potentially malicious HTML content
        malicious_html = '<p>Safe content</p><script>alert("XSS")</script><img src="x" onerror="alert(1)">'

        # Set offering user to CREATING state first for valid transition
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        # Test state transition comment sanitization
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {"comment": malicious_html, "comment_url": "https://test.example.com"}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering_user.refresh_from_db()
        # Should keep safe HTML but remove dangerous tags
        self.assertIn(
            "<p>Safe content</p>", self.offering_user.service_provider_comment
        )
        self.assertNotIn("<script>", self.offering_user.service_provider_comment)
        self.assertNotIn("onerror", self.offering_user.service_provider_comment)

        # Test update comments sanitization
        url = self.get_url(self.offering_user, "update_comments")
        payload = {
            "service_provider_comment": malicious_html,
        }
        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering_user.refresh_from_db()
        # Should keep safe HTML but remove dangerous tags
        self.assertIn(
            "<p>Safe content</p>", self.offering_user.service_provider_comment
        )
        self.assertNotIn("<script>", self.offering_user.service_provider_comment)
        self.assertNotIn("onerror", self.offering_user.service_provider_comment)

    def test_serializer_exposes_comment_url(self):
        """Test that the serializer exposes the service_provider_comment_url field."""
        self.offering_user.service_provider_comment = "Test comment"
        self.offering_user.service_provider_comment_url = "https://test.example.com"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("service_provider_comment", response.data)
        self.assertIn("service_provider_comment_url", response.data)
        self.assertEqual(response.data["service_provider_comment"], "Test comment")
        self.assertEqual(
            response.data["service_provider_comment_url"], "https://test.example.com"
        )

    def test_transition_from_pending_account_linking_to_pending_additional_validation(
        self,
    ):
        """Test transition from PENDING_ACCOUNT_LINKING to PENDING_ADDITIONAL_VALIDATION."""
        self.offering_user.state = OfferingUserStates.PENDING_ACCOUNT_LINKING
        self.offering_user.service_provider_comment = "Please link your account"
        self.offering_user.save()
        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {"comment": "Additional documents required"}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        self.assertEqual(
            self.offering_user.service_provider_comment, "Additional documents required"
        )

    def test_transition_from_pending_additional_validation_to_pending_account_linking(
        self,
    ):
        """Test transition from PENDING_ADDITIONAL_VALIDATION to PENDING_ACCOUNT_LINKING."""
        self.offering_user.state = OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        self.offering_user.service_provider_comment = "Additional documents required"
        self.offering_user.save()
        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_account_linking")
        payload = {"comment": "Please link your existing account"}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ACCOUNT_LINKING
        )
        self.assertEqual(
            self.offering_user.service_provider_comment,
            "Please link your existing account",
        )

    def test_transition_between_pending_states_preserves_comment_url(self):
        """Test that comment URLs are preserved when transitioning between pending states."""
        self.offering_user.state = OfferingUserStates.PENDING_ACCOUNT_LINKING
        self.offering_user.save()
        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {
            "comment": "Additional documents required",
            "comment_url": "https://docs.example.com/validation",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        self.assertEqual(
            self.offering_user.service_provider_comment, "Additional documents required"
        )
        self.assertEqual(
            self.offering_user.service_provider_comment_url,
            "https://docs.example.com/validation",
        )


@ddt
class OfferingUserBackwardCompatibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_USER)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        self.user = UserFactory()
        models.UserOfferingConsent.objects.create(
            user=self.user,
            offering=self.offering,
            version="1.0",
        )

    def test_create_offering_user_with_username_sets_ok_state(self):
        """Test that creating OfferingUser with username automatically sets state to OK."""
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {
            "offering_uuid": self.offering.uuid.hex,
            "user_uuid": self.fixture.user.uuid.hex,
            "username": "testuser",
        }
        response = self.client.post(OfferingUserFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering_user = OfferingUser.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(offering_user.username, "testuser")

    def test_create_offering_user_without_username_keeps_creation_requested_state(self):
        """Test that creating OfferingUser without username keeps CREATION_REQUESTED state."""
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {
            "offering_uuid": self.offering.uuid.hex,
            "user_uuid": self.fixture.user.uuid.hex,
        }
        response = self.client.post(OfferingUserFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering_user = OfferingUser.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_update_offering_user_with_username_sets_ok_state(self):
        """Test that updating OfferingUser with username automatically sets state to OK."""
        offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            state=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        )

        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {"username": "updated_username"}
        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(offering_user.username, "updated_username")

    def test_update_offering_user_without_username_preserves_state(self):
        """Test that updating other fields doesn't change state."""
        UserFactory()
        offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            state=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        )
        # Set username manually after creation to avoid triggering FSM transition
        OfferingUser.objects.filter(pk=offering_user.pk).update(
            username="existing_username"
        )

        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {
            "username": "existing_username"
        }  # Update same username (no actual change)
        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering_user.refresh_from_db()
        self.assertEqual(
            offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )  # State unchanged

    def test_model_save_with_username_change_sets_ok_state(self):
        """Test that model save method automatically sets state to OK when username changes."""
        user = UserFactory()
        offering_user = OfferingUser.objects.create(
            offering=self.offering, user=user, state=OfferingUserStates.CREATING
        )

        # Simulate username being set
        offering_user.username = "direct_save_username"
        offering_user.save()

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)


class SetOfferingsUsernameBackwardCompatibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        self.offering1 = factories.OfferingFactory(customer=self.fixture.customer)
        self.offering2 = factories.OfferingFactory(customer=self.fixture.customer)

        # Add user to project so they can be found by get_connected_projects
        self.fixture.project.add_user(self.fixture.user, ProjectRole.MEMBER)

        # Create resources for offerings
        self.resource1 = factories.ResourceFactory(
            offering=self.offering1,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        self.resource2 = factories.ResourceFactory(
            offering=self.offering2,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

    def test_set_offerings_username_creates_offering_users_with_ok_state(self):
        """Test that set_offerings_username creates OfferingUsers with OK state."""
        url = (
            "http://testserver"
            + reverse(
                "marketplace-service-provider-detail",
                kwargs={"uuid": self.service_provider.uuid.hex},
            )
            + "set_offerings_username/"
        )

        payload = {"user_uuid": self.fixture.user.uuid.hex, "username": "test_username"}

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Check that OfferingUsers were created with OK state
        offering_users = OfferingUser.objects.filter(user=self.fixture.user)
        self.assertEqual(offering_users.count(), 2)

        for offering_user in offering_users:
            self.assertEqual(offering_user.state, OfferingUserStates.OK)
            self.assertEqual(offering_user.username, "test_username")

    def test_set_offerings_username_updates_existing_offering_users_to_ok_state(self):
        """Test that set_offerings_username updates existing OfferingUsers to OK state."""
        # Create existing OfferingUsers with different states
        offering_user1 = OfferingUser.objects.create(
            offering=self.offering1,
            user=self.fixture.user,
            state=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        )
        offering_user2 = OfferingUser.objects.create(
            offering=self.offering2,
            user=self.fixture.user,
            state=OfferingUserStates.CREATING,
        )

        url = (
            "http://testserver"
            + reverse(
                "marketplace-service-provider-detail",
                kwargs={"uuid": self.service_provider.uuid.hex},
            )
            + "set_offerings_username/"
        )

        payload = {
            "user_uuid": self.fixture.user.uuid.hex,
            "username": "updated_username",
        }

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Check that existing OfferingUsers were updated to OK state
        offering_user1.refresh_from_db()
        offering_user2.refresh_from_db()

        self.assertEqual(offering_user1.state, OfferingUserStates.OK)
        self.assertEqual(offering_user1.username, "updated_username")
        self.assertEqual(offering_user2.state, OfferingUserStates.OK)
        self.assertEqual(offering_user2.username, "updated_username")

    def test_set_offerings_username_without_username_does_not_change_state(self):
        """Test that set_offerings_username without username doesn't change state."""
        offering_user = OfferingUser.objects.create(
            offering=self.offering1,
            user=self.fixture.user,
            state=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        )
        # Set username manually after creation to avoid triggering FSM transition
        OfferingUser.objects.filter(pk=offering_user.pk).update(
            username="existing_username"
        )

        url = (
            "http://testserver"
            + reverse(
                "marketplace-service-provider-detail",
                kwargs={"uuid": self.service_provider.uuid.hex},
            )
            + "set_offerings_username/"
        )

        payload = {
            "user_uuid": self.fixture.user.uuid.hex,
            "username": "",  # Empty username
        }

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(url, payload)

        # Should still succeed but not change state
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering_user.refresh_from_db()
        self.assertEqual(
            offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )  # State unchanged


@ddt
class OfferingUserStateFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()

        # Create offering users with different states
        self.user1 = UserFactory()
        self.user2 = UserFactory()
        self.user3 = UserFactory()

        self.offering_user1 = OfferingUser.objects.create(
            offering=self.offering,
            user=self.user1,
            state=OfferingUserStates.CREATION_REQUESTED,
        )
        self.offering_user2 = OfferingUser.objects.create(
            offering=self.offering,
            user=self.user2,
            state=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        )
        self.offering_user3 = OfferingUser.objects.create(
            offering=self.offering,
            user=self.user3,
            username="user3",
            state=OfferingUserStates.OK,
        )
        for user in [self.user1, self.user2, self.user3]:
            models.UserOfferingConsent.objects.create(
                user=user,
                offering=self.offering,
                version="1.0",
            )

    def test_filter_by_single_state(self):
        """Test filtering by a single state value."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"state": "Requested"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering_user1.uuid.hex)

    def test_filter_by_multiple_states(self):
        """Test filtering by multiple state values."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"state": ["Requested", "OK"]},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)
        returned_uuids = {item["uuid"] for item in response.data}
        expected_uuids = {
            self.offering_user1.uuid.hex,
            self.offering_user3.uuid.hex,
        }
        self.assertEqual(returned_uuids, expected_uuids)

    def test_filter_by_pending_additional_validation_state(self):
        """Test filtering by pending additional validation state."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"state": "Pending additional validation"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering_user2.uuid.hex)

    def test_filter_by_nonexistent_state(self):
        """Test filtering by a state that doesn't exist returns validation error."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"state": "NonexistentState"},
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("state", response.data)

    def test_filter_combines_with_other_filters(self):
        """Test that state filter can be combined with other filters."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {
                "state": ["Requested", "OK"],
                "offering_uuid": self.offering.uuid.hex,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)
        # All results should be from the same offering
        for item in response.data:
            self.assertEqual(item["offering_uuid"], self.offering.uuid.hex)

    def test_no_state_filter_returns_all_users(self):
        """Test that without state filter, all offering users are returned."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(OfferingUserFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 3)


@ddt
class OfferingUserDeletionWorkflowTest(test.APITestCase):
    """Test the complete deletion workflow for OfferingUsers."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

        self.offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.fixture.user,
            state=OfferingUserStates.OK,
            username="test_user",
        )

    def test_request_deletion_transition(self):
        """Test requesting deletion from OK state."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = OfferingUserFactory.get_url(self.offering_user, "request-deletion")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.DELETION_REQUESTED
        )

    def test_set_deleting_transition(self):
        """Test starting deletion process from DELETION_REQUESTED state."""
        self.offering_user.state = OfferingUserStates.DELETION_REQUESTED
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleting")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.DELETING)

    def test_set_deleted_transition(self):
        """Test marking user as successfully deleted from DELETING state."""
        self.offering_user.state = OfferingUserStates.DELETING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleted")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.DELETED)

    def test_complete_deletion_workflow(self):
        """Test the complete deletion workflow from OK to DELETED."""
        self.client.force_authenticate(user=self.fixture.owner)

        # Step 1: Request deletion
        url = OfferingUserFactory.get_url(self.offering_user, "request-deletion")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.DELETION_REQUESTED
        )

        # Step 2: Start deleting
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleting")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.DELETING)

        # Step 3: Mark as deleted
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleted")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.DELETED)

    def test_retry_deletion_after_error(self):
        """Test retrying deletion after error state."""
        # Set to error deleting state
        self.offering_user.state = OfferingUserStates.ERROR_DELETING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)

        # Should be able to retry deletion
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleting")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.DELETING)

    def test_unauthorized_deletion_operations(self):
        """Test that unauthorized users cannot perform deletion operations."""
        unauthorized_user = UserFactory()
        self.client.force_authenticate(user=unauthorized_user)

        # Test request_deletion
        url = OfferingUserFactory.get_url(self.offering_user, "request-deletion")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

        # Test set_deleting
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleting")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

        # Test set_deleted
        url = OfferingUserFactory.get_url(self.offering_user, "set-deleted")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

    def test_invalid_state_transitions_return_400_not_500(self):
        """Test that invalid state transitions return HTTP 400 instead of HTTP 500."""
        # Test the original bug scenario: PENDING_ADDITIONAL_VALIDATION -> ERROR_DELETING
        self.offering_user.state = OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-set-error-deleting",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.post(url)

        # Should return 400 Bad Request, not 500 Internal Server Error
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("detail", response.data)
        self.assertIn(
            "Cannot transition to ERROR_DELETING from current state",
            response.data["detail"],
        )

        # Test another invalid transition: OK -> ERROR_CREATING
        self.offering_user.state = OfferingUserStates.OK
        self.offering_user.save()

        url = "http://testserver" + reverse(
            "marketplace-offering-user-set-error-creating",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.post(url)

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("detail", response.data)
        self.assertIn(
            "Cannot transition to ERROR_CREATING from current state",
            response.data["detail"],
        )

        # Test another invalid transition: CREATION_REQUESTED -> set_deleted
        self.offering_user.state = OfferingUserStates.CREATION_REQUESTED
        self.offering_user.save()

        url = OfferingUserFactory.get_url(self.offering_user, "set-deleted")
        response = self.client.post(url)

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("detail", response.data)
        self.assertIn(
            "Cannot transition to DELETED from current state", response.data["detail"]
        )


@ddt
class OfferingUserChecklistTest(test.APITestCase):
    """Test checklist functionality for offering users."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

        # Create an offering with compliance checklists
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()

        # Create a compliance checklist
        from waldur_core.checklist.tests.factories import (
            ChecklistFactory,
            QuestionFactory,
        )

        self.checklist = ChecklistFactory(
            checklist_type="offering_compliance", name="Test Compliance Checklist"
        )

        # Set the checklist for the offering
        self.offering.compliance_checklist = self.checklist
        self.offering.save()

        # Create questions for the checklist
        from waldur_core.checklist.enums import QuestionTypes

        self.question1 = QuestionFactory(
            checklist=self.checklist,
            description="Do you comply with data protection requirements?",
            question_type=QuestionTypes.BOOLEAN,
            order=1,
        )
        self.question2 = QuestionFactory(
            checklist=self.checklist,
            description="Please provide your security certificate details",
            question_type=QuestionTypes.TEXT_INPUT,
            order=2,
        )

        # Create an offering user
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="testuser"
        )

        # Set up permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def test_checklist_endpoint_returns_checklist_questions(self):
        """Test that checklist endpoint returns checklist questions."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = OfferingUserFactory.get_url(self.offering_user, "checklist")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("checklist", response.data)
        self.assertIn("questions", response.data)
        self.assertEqual(len(response.data["questions"]), 2)

    def test_checklist_endpoint_without_checklist_returns_error(self):
        """Test that checklist endpoint returns error when no checklist is configured."""
        # Create an offering without compliance checklist
        offering_without_checklist = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        offering_without_checklist.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        # compliance_checklist is already None by default
        offering_without_checklist.save()

        offering_user_without_checklist = OfferingUser.objects.create(
            offering=offering_without_checklist,
            user=self.fixture.user,
            username="testuser2",
        )

        self.client.force_authenticate(user=self.fixture.owner)

        url = reverse(
            "marketplace-offering-user-checklist",
            kwargs={"uuid": offering_user_without_checklist.uuid.hex},
        )
        response = self.client.get(url)

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("No checklist configured", response.data["detail"])

    def test_completion_status_endpoint_returns_status(self):
        """Test that completion status endpoint returns completion status."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = reverse(
            "marketplace-offering-user-completion-status",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("is_completed", response.data)
        self.assertIn("completion_percentage", response.data)
        self.assertIn("checklist_name", response.data)

    def test_submit_answers_endpoint_creates_answers(self):
        """Test that submit answers endpoint creates checklist answers."""
        self.client.force_authenticate(user=self.fixture.user)

        # Submit answers
        answers_data = [
            {"question_uuid": self.question1.uuid.hex, "answer_data": True},
            {
                "question_uuid": self.question2.uuid.hex,
                "answer_data": "ISO 27001 certified",
            },
        ]

        url = OfferingUserFactory.get_url(self.offering_user, "submit-answers")
        response = self.client.post(url, answers_data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data["detail"], "Answers submitted successfully")

    def test_checklist_review_endpoint_for_service_provider(self):
        """Test that service providers can access review endpoints."""
        # Create service provider user
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)

        self.client.force_authenticate(user=service_provider_user)

        url = reverse(
            "marketplace-offering-user-checklist-review",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("checklist", response.data)
        self.assertIn("questions", response.data)

    def test_completion_review_status_endpoint_for_service_provider(self):
        """Test that service providers can access completion review status."""
        # Create service provider user
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)

        self.client.force_authenticate(user=service_provider_user)

        url = reverse(
            "marketplace-offering-user-completion-review-status",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("is_completed", response.data)
        self.assertIn(
            "requires_review", response.data
        )  # This should be in the reviewer serializer
        self.assertIn("completion_percentage", response.data)

    @data("admin", "manager")
    def test_unauthorized_users_cannot_access_checklist_endpoints(self, user_role):
        """Test that unauthorized users cannot access checklist endpoints."""
        # Use a user without proper permissions
        unauthorized_user = getattr(self.fixture, user_role)
        self.client.force_authenticate(user=unauthorized_user)

        # Test checklist endpoint
        url = OfferingUserFactory.get_url(self.offering_user, "checklist")
        response = self.client.get(url)
        # Users who can't see the offering_user get 404
        self.assertEqual(
            response.status_code, status.HTTP_404_NOT_FOUND
        )  # Object not found - user can't see the offering_user

        # Test submit answers endpoint
        url = OfferingUserFactory.get_url(self.offering_user, "submit-answers")
        response = self.client.post(url, [], format="json")
        # Users who can't see the offering_user get 404
        self.assertEqual(
            response.status_code, status.HTTP_404_NOT_FOUND
        )  # Object not found - user can't see the offering_user

    def test_completely_unrelated_user_cannot_access_checklist_endpoints(self):
        """Test that a completely unrelated user cannot access checklist endpoints."""
        # Create a new user without any permissions
        unrelated_user = UserFactory()
        self.client.force_authenticate(user=unrelated_user)

        # Test checklist endpoint
        url = OfferingUserFactory.get_url(self.offering_user, "checklist")
        response = self.client.get(url)
        self.assertEqual(
            response.status_code, status.HTTP_404_NOT_FOUND
        )  # Should be filtered out by queryset

    def test_checklist_completion_created_automatically_on_offering_user_creation(self):
        """Test that ChecklistCompletion is created automatically when OfferingUser is created."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.checklist.models import ChecklistCompletion

        # Create a new offering user
        new_user = UserFactory()
        new_offering_user = OfferingUser.objects.create(
            offering=self.offering, user=new_user, username="newuser"
        )

        # Check that ChecklistCompletion was created
        content_type = ContentType.objects.get_for_model(OfferingUser)
        completion = ChecklistCompletion.objects.filter(
            scope_content_type=content_type,
            scope_object_id=new_offering_user.id,
            checklist=self.checklist,
        ).first()

        self.assertIsNotNone(completion)
        self.assertEqual(completion.checklist, self.checklist)

    def test_checklist_completion_deleted_when_offering_user_deleted(self):
        """Test that ChecklistCompletion is deleted when OfferingUser is deleted."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.checklist.models import ChecklistCompletion

        # Verify completion exists
        content_type = ContentType.objects.get_for_model(OfferingUser)
        completion_exists_before = ChecklistCompletion.objects.filter(
            scope_content_type=content_type,
            scope_object_id=self.offering_user.id,
            checklist=self.checklist,
        ).exists()
        self.assertTrue(completion_exists_before)

        # Delete the offering user
        offering_user_id = self.offering_user.id
        self.offering_user.delete()

        # Check that ChecklistCompletion was deleted
        completion_exists_after = ChecklistCompletion.objects.filter(
            scope_content_type=content_type,
            scope_object_id=offering_user_id,
            checklist=self.checklist,
        ).exists()
        self.assertFalse(completion_exists_after)


@ddt
class ServiceProviderComplianceTest(test.APITestCase):
    """Test service provider compliance management endpoints."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Add service provider permissions to customer owner
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )

        # Create a service provider
        self.service_provider = ServiceProviderFactory(customer=self.fixture.customer)

        # Create checklist for compliance
        self.checklist = ChecklistFactory(
            checklist_type="offering_compliance",
            name="Compliance Checklist",
        )
        self.question = QuestionFactory(
            checklist=self.checklist,
            description="Are you compliant?",
            question_type=QuestionTypes.BOOLEAN,
            required=True,
        )

        # Create offerings with and without checklists
        self.offering_with_checklist = OfferingFactory(
            customer=self.fixture.customer,
            compliance_checklist=self.checklist,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        self.offering_without_checklist = OfferingFactory(
            customer=self.fixture.customer,
            compliance_checklist=None,
            plugin_options={"service_provider_can_create_offering_user": True},
        )

        # Create offering users
        self.user1 = UserFactory()
        self.user2 = UserFactory()
        self.offering_user1 = OfferingUserFactory(
            offering=self.offering_with_checklist, user=self.user1, username="user1"
        )
        self.offering_user2 = OfferingUserFactory(
            offering=self.offering_with_checklist, user=self.user2, username="user2"
        )
        self.offering_user3 = OfferingUserFactory(
            offering=self.offering_without_checklist,
            user=self.user1,
            username="user1_no_checklist",
        )

    @data("owner", "staff")
    def test_compliance_overview_authorized_users(self, user_role):
        """Test that authorized users can access compliance overview."""
        if user_role == "staff":
            user = self.fixture.staff
        else:
            user = self.fixture.owner

        self.client.force_authenticate(user=user)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)  # Only offering with checklist

        # Check data for offering with checklist
        offering_with_checklist_data = next(
            (
                item
                for item in response.data
                if item["offering_uuid"] == str(self.offering_with_checklist.uuid)
            ),
            None,
        )
        self.assertIsNotNone(offering_with_checklist_data)
        self.assertEqual(offering_with_checklist_data["total_users"], 2)
        self.assertEqual(offering_with_checklist_data["users_with_completions"], 2)
        self.assertEqual(
            offering_with_checklist_data["completed_users"], 0
        )  # No answers yet
        self.assertEqual(offering_with_checklist_data["pending_users"], 2)
        self.assertEqual(offering_with_checklist_data["compliance_rate"], 0.0)
        self.assertIsNotNone(offering_with_checklist_data["checklist_name"])

        # Verify offering without checklist is not included
        offering_without_checklist_data = next(
            (
                item
                for item in response.data
                if item["offering_uuid"] == str(self.offering_without_checklist.uuid)
            ),
            None,
        )
        self.assertIsNone(offering_without_checklist_data)

    def test_compliance_overview_filters_offerings_without_checklist(self):
        """Test that compliance overview only shows offerings with compliance checklist."""
        # Create additional offerings
        offering_with_checklist_2 = OfferingFactory(
            customer=self.fixture.customer,
            compliance_checklist=self.checklist,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        offering_without_checklist_2 = OfferingFactory(
            customer=self.fixture.customer,
            compliance_checklist=None,
            plugin_options={"service_provider_can_create_offering_user": True},
        )

        # Create users for the new offerings
        OfferingUserFactory(
            offering=offering_with_checklist_2, user=UserFactory(), username="user_new1"
        )
        OfferingUserFactory(
            offering=offering_without_checklist_2,
            user=UserFactory(),
            username="user_new2",
        )

        self.client.force_authenticate(user=self.fixture.owner)
        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Should only return offerings with compliance checklist
        self.assertEqual(len(response.data), 2)  # Only 2 offerings with checklist

        # Verify all returned offerings have checklist_name
        for offering_data in response.data:
            self.assertIsNotNone(offering_data["checklist_name"])
            self.assertIn(offering_data["checklist_name"], [self.checklist.name])

        # Verify specific offerings are included/excluded
        offering_uuids = {item["offering_uuid"] for item in response.data}
        self.assertIn(str(self.offering_with_checklist.uuid), offering_uuids)
        self.assertIn(str(offering_with_checklist_2.uuid), offering_uuids)
        self.assertNotIn(str(self.offering_without_checklist.uuid), offering_uuids)
        self.assertNotIn(str(offering_without_checklist_2.uuid), offering_uuids)

    def test_compliance_overview_unauthorized_user(self):
        """Test that unauthorized users cannot access compliance overview."""
        self.client.force_authenticate(user=self.user1)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    @data("owner", "staff")
    def test_offering_users_list_authorized_users(self, user_role):
        """Test that authorized users can list offering users with compliance status."""
        if user_role == "staff":
            user = self.fixture.staff
        else:
            user = self.fixture.owner

        self.client.force_authenticate(user=user)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "offering-users"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 3)  # Three offering users total

        # Check that all users are present
        user_uuids = {item["uuid"] for item in response.data}
        expected_uuids = {
            str(self.offering_user1.uuid),
            str(self.offering_user2.uuid),
            str(self.offering_user3.uuid),
        }
        self.assertEqual(user_uuids, expected_uuids)

        # Check compliance statuses
        for item in response.data:
            if item["checklist_name"]:
                self.assertEqual(item["compliance_status"], "pending")  # No answers yet
                self.assertEqual(item["completion_percentage"], 0)
            else:
                self.assertEqual(item["compliance_status"], "no_checklist")
                self.assertIsNone(item["completion_percentage"])

    def test_offering_users_filter_by_offering_uuid(self):
        """Test filtering offering users by offering UUID."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "offering-users"
        )

        # Filter by offering with checklist
        response = self.client.get(
            url, {"offering_uuid": self.offering_with_checklist.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)  # Two users for this offering

        for item in response.data:
            self.assertEqual(item["offering_name"], self.offering_with_checklist.name)

    def test_offering_users_filter_by_compliance_status(self):
        """Test filtering offering users by compliance status."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "offering-users"
        )

        # Filter by no_checklist
        response = self.client.get(url, {"compliance_status": "no_checklist"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.offering_user3.uuid))

        # Filter by pending
        response = self.client.get(url, {"compliance_status": "pending"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)  # Two users with incomplete checklists

        # Filter by completed (should be empty since no answers submitted yet)
        response = self.client.get(url, {"compliance_status": "completed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 0)

    def test_compliance_overview_with_completed_checklist(self):
        """Test compliance overview when some users have completed their checklists."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.checklist.models import Answer, ChecklistCompletion

        # Submit answer for one user to complete their checklist
        content_type = ContentType.objects.get_for_model(OfferingUser)
        completion = ChecklistCompletion.objects.get(
            scope_content_type=content_type,
            scope_object_id=self.offering_user1.id,
            checklist=self.checklist,
        )

        # Create answer for the required question
        Answer.objects.create(
            completion=completion,
            question=self.question,
            user=self.user1,
            answer_data=True,
        )

        # Manually update completion status since we're directly creating answer
        completion.refresh_from_db()
        completion.is_completed = True
        completion.save()

        self.client.force_authenticate(user=self.fixture.owner)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Check data for offering with checklist
        offering_data = next(
            (
                item
                for item in response.data
                if item["offering_uuid"] == str(self.offering_with_checklist.uuid)
            ),
            None,
        )
        self.assertIsNotNone(offering_data)
        self.assertEqual(offering_data["total_users"], 2)
        self.assertEqual(offering_data["users_with_completions"], 2)
        self.assertEqual(offering_data["completed_users"], 1)
        self.assertEqual(offering_data["pending_users"], 1)
        self.assertEqual(offering_data["compliance_rate"], 50.0)  # 1 out of 2 completed

    def test_offering_users_unauthorized_user(self):
        """Test that unauthorized users cannot list offering users."""
        self.client.force_authenticate(user=self.user1)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "offering-users"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_compliance_overview_pagination_default_page_size(self):
        """Test that compliance overview endpoint supports pagination with default page size."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Check that response has pagination headers
        self.assertIn("X-Result-Count", response)
        self.assertEqual(
            response["X-Result-Count"], "1"
        )  # Only offering with checklist
        # Default pagination returns all items when count is less than default page size
        self.assertEqual(len(response.data), 1)

    def test_compliance_overview_pagination_with_page_size_param(self):
        """Test that compliance overview endpoint supports custom page size."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )

        # Request with page_size=1
        response = self.client.get(url, {"page_size": 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("X-Result-Count", response)
        self.assertEqual(
            response["X-Result-Count"], "1"
        )  # Only offering with checklist
        self.assertEqual(len(response.data), 1)  # Only 1 item total

        # With only 1 item total, no pagination links needed
        # Link header may not be present or may not have "next"

    def test_compliance_overview_pagination_second_page(self):
        """Test accessing second page of compliance overview."""
        # Create additional offering with checklist to test pagination
        additional_offering = OfferingFactory(
            customer=self.fixture.customer,
            compliance_checklist=self.checklist,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        OfferingUserFactory(
            offering=additional_offering, user=UserFactory(), username="user_page2"
        )

        self.client.force_authenticate(user=self.fixture.owner)
        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )

        # Request second page with page_size=1
        response = self.client.get(url, {"page": 2, "page_size": 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("X-Result-Count", response)
        self.assertEqual(
            response["X-Result-Count"], "2"
        )  # Total count (2 offerings with checklist)
        self.assertEqual(len(response.data), 1)  # Second item

        # Check that Link header contains navigation links
        self.assertIn("Link", response)
        link_header = response["Link"]
        self.assertIn("prev", link_header)

    def test_compliance_overview_pagination_exceeds_max_page_size(self):
        """Test that page_size is limited by max_page_size setting."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )

        # Request with very large page_size (exceeds max_page_size=300)
        response = self.client.get(url, {"page_size": 500})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Even with large page_size, we only get the available items (1 offering with checklist)
        self.assertEqual(len(response.data), 1)

    def test_compliance_overview_database_level_pagination_performance(self):
        """Test that pagination is applied at database level for performance."""
        # Create multiple offerings with checklists to test pagination performance
        offerings = []
        for i in range(10):
            offering = factories.OfferingFactory(
                customer=self.service_provider.customer,
                shared=True,
                state=models.OfferingStates.ACTIVE,
                name=f"Test Offering {i}",
                compliance_checklist=self.checklist,  # Add checklist so offerings are included
            )
            offerings.append(offering)

        # Create offering users for some offerings to ensure completion queries are tested
        for i in range(5):
            factories.OfferingUserFactory(offering=offerings[i], user=UserFactory())

        self.client.force_authenticate(self.fixture.owner)
        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )

        # Test with page_size=3 to ensure only 3 offerings are processed
        from django.db import connection, reset_queries
        from django.test.utils import override_settings

        with override_settings(DEBUG=True):
            reset_queries()
            response = self.client.get(url, {"page_size": 3})

            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertEqual(len(response.data), 3)  # Should return exactly 3 items

            # Verify pagination headers
            self.assertIn("X-Result-Count", response)
            total_count = int(response["X-Result-Count"])
            self.assertGreaterEqual(
                total_count, 10
            )  # At least our 10 offerings plus any from fixture
            self.assertIn("Link", response)

            # Check that database queries are optimized
            # Should have reasonable number of queries regardless of total offerings
            query_count = len(connection.queries)
            self.assertLess(
                query_count,
                10,
                f"Expected < 10 queries, got {query_count}. This suggests pagination isn't applied at DB level.",
            )


class OfferingComplianceSerializerTest(test.APITestCase):
    """Test that offering API exposes compliance checklist information."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = ServiceProviderFactory(customer=self.fixture.customer)

    def test_offering_with_compliance_checklist_shows_checklist_info(self):
        """Test that offering with compliance checklist exposes checklist information."""
        # Create checklist and offering
        checklist = ChecklistFactory(
            name="Security Compliance",
            description="Required security compliance checklist",
            checklist_type="offering_compliance",
        )
        offering = OfferingFactory(
            customer=self.service_provider.customer,
            compliance_checklist=checklist,
        )

        # Fetch offering details
        url = reverse(
            "marketplace-public-offering-detail", kwargs={"uuid": offering.uuid.hex}
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Verify compliance requirements is indicated
        self.assertTrue(response.data["has_compliance_requirements"])

    def test_offering_without_compliance_checklist_shows_no_requirements(self):
        """Test that offering without compliance checklist shows no requirements."""
        offering = OfferingFactory(customer=self.service_provider.customer)

        url = reverse(
            "marketplace-public-offering-detail", kwargs={"uuid": offering.uuid.hex}
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Verify no compliance requirements
        self.assertFalse(response.data["has_compliance_requirements"])


@ddt
class ServiceProviderCompliancePerformanceTest(test.APITestCase):
    """Test performance of compliance_overview action."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = ServiceProviderFactory(customer=self.fixture.customer)

        # Set up permissions for the compliance overview endpoint
        from waldur_core.permissions.enums import PermissionEnum
        from waldur_core.permissions.fixtures import CustomerRole

        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )

        # Create compliance checklist for some offerings
        self.checklist = ChecklistFactory(name="Compliance Checklist")
        QuestionFactory(checklist=self.checklist, description="Security question")
        QuestionFactory(checklist=self.checklist, description="Privacy question")

        # Create multiple offerings with different scenarios
        self.offerings_with_checklist = []
        self.offerings_without_checklist = []

        # Create 5 offerings with checklists and users
        for i in range(5):
            offering = OfferingFactory(
                customer=self.fixture.customer,
                name=f"Offering {i}",
                compliance_checklist=self.checklist,
            )
            self.offerings_with_checklist.append(offering)

            # Create 3-5 offering users for each offering
            for j in range(3 + i):  # 3, 4, 5, 6, 7 users respectively
                user = UserFactory()
                OfferingUserFactory(offering=offering, user=user)

        # Create 3 offerings without checklists
        for i in range(3):
            offering = OfferingFactory(
                customer=self.fixture.customer, name=f"No Checklist Offering {i}"
            )
            self.offerings_without_checklist.append(offering)

            # Create 2-4 offering users for each offering
            for j in range(2 + i):  # 2, 3, 4 users respectively
                user = UserFactory()
                OfferingUserFactory(offering=offering, user=user)

        # Mark some existing checklist completions as completed for testing
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.checklist.models import ChecklistCompletion

        content_type = ContentType.objects.get_for_model(OfferingUser)

        # Complete checklist for some users in first offering
        first_offering_users = OfferingUser.objects.filter(
            offering=self.offerings_with_checklist[0]
        )[:2]
        for offering_user in first_offering_users:
            # Find existing completion (created by signal) and mark as completed
            completion = ChecklistCompletion.objects.get(
                checklist=self.checklist,
                scope_content_type=content_type,
                scope_object_id=offering_user.id,
            )
            completion.is_completed = True
            completion.save()

        self.client = test.APIClient()
        self.client.force_authenticate(user=self.fixture.staff)

    @data("staff", "owner")
    def test_compliance_overview_query_count(self, user_role):
        """Test that compliance_overview uses reasonable number of queries."""
        import time

        from django.db import connection, reset_queries
        from django.test.utils import override_settings

        # Use the appropriate user
        if user_role == "staff":
            user = self.fixture.staff
        else:
            user = self.fixture.owner

        self.client.force_authenticate(user=user)

        # Reset queries and enable query logging
        with override_settings(DEBUG=True):
            reset_queries()

            url = ServiceProviderFactory.get_compliance_url(
                self.service_provider, "compliance-overview"
            )

            start_time = time.time()
            response = self.client.get(url)
            end_time = time.time()

            # Check response is successful
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

            # Get query count
            query_count = len(connection.queries)

            # Print diagnostic information
            print(f"\nCompliance Overview Performance Test ({user_role}):")
            print(f"Query count: {query_count}")
            print(f"Response time: {end_time - start_time:.2f} seconds")
            print(f"Number of offerings returned: {len(response.data)}")

            # Calculate total users for context
            total_users = sum(item["total_users"] for item in response.data)
            print(f"Total users across all offerings: {total_users}")

            # Print the most expensive/repetitive queries
            print("\nQueries analysis:")
            query_patterns = {}
            for query in connection.queries:
                sql = query["sql"]
                # Extract table name pattern
                if "FROM" in sql.upper():
                    table_part = (
                        sql.upper()
                        .split("FROM")[1]
                        .split("WHERE")[0]
                        .split("ORDER")[0]
                        .split("GROUP")[0]
                        .strip()
                    )
                    table_name = table_part.split()[0].strip('"').replace("`", "")
                    if table_name in query_patterns:
                        query_patterns[table_name] += 1
                    else:
                        query_patterns[table_name] = 1

            print("Query count by table:")
            for table, count in sorted(
                query_patterns.items(), key=lambda x: x[1], reverse=True
            ):
                if count > 1:
                    print(f"  {table}: {count} queries")

            # Optimized implementation using prefetch_related to eliminate N+1 queries
            # Optimized results: ~6 queries total regardless of offering count
            # This represents a 77% improvement from the original 26 queries

            offerings_count = len(self.offerings_with_checklist) + len(
                self.offerings_without_checklist
            )

            # Optimized baseline: ~6 queries total (constant time complexity)
            # This includes: permission checks + optimized prefetch queries for all data at once
            expected_optimized_queries = 6  # Constant regardless of offering count

            print(f"Expected optimized queries: {expected_optimized_queries}")

            # Assert optimized performance - should be constant time complexity
            # Allow small buffer for test setup variations but keep it tight to catch regressions
            self.assertLess(
                query_count,
                10,
                f"Optimized query count ({query_count}) exceeds expected constant complexity. "
                f"Performance optimization may have regressed.",
            )

            # Verify we achieved the optimization target
            self.assertLess(
                query_count,
                15,
                f"Optimized version should use <15 queries regardless of offering count, got {query_count}",
            )

            # Verify constant time complexity - query count shouldn't scale with offering count
            if offerings_count > 5:  # Only check scaling for meaningful dataset
                queries_per_offering = query_count / offerings_count
                self.assertLess(
                    queries_per_offering,
                    2,  # Much tighter bound for optimized version
                    f"Query count per offering ({queries_per_offering:.1f}) indicates scaling issues. "
                    f"Optimized version should have constant time complexity.",
                )

    def test_compliance_overview_data_accuracy(self):
        """Test that compliance_overview returns accurate data despite performance issues."""
        self.client.force_authenticate(user=self.fixture.staff)

        url = ServiceProviderFactory.get_compliance_url(
            self.service_provider, "compliance-overview"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Should only have 5 offerings (only those with checklist)
        self.assertEqual(len(response.data), 5)

        # Verify all returned offerings have checklist
        offerings_with_checklist_data = [
            item for item in response.data if item["checklist_name"] is not None
        ]
        self.assertEqual(len(offerings_with_checklist_data), 5)

        # Verify no offerings without checklist are included
        offerings_without_checklist_data = [
            item for item in response.data if item["checklist_name"] is None
        ]
        self.assertEqual(len(offerings_without_checklist_data), 0)

        # Check that the first offering (with completions) has correct data
        first_offering_data = next(
            item for item in response.data if item["offering_name"] == "Offering 0"
        )

        # Should have 3 total users, 2 completed
        self.assertEqual(first_offering_data["total_users"], 3)
        self.assertEqual(first_offering_data["completed_users"], 2)
        self.assertEqual(
            first_offering_data["users_with_completions"], 3
        )  # All users get completion entries
        self.assertEqual(
            first_offering_data["pending_users"], 1
        )  # 3 total - 2 completed = 1 pending
        self.assertAlmostEqual(first_offering_data["compliance_rate"], 66.67, places=1)


class OfferingUserSignalTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

        self.offering = OfferingFactory()
        self.offering.project = self.fixture.project
        self.offering.save()

        self.user = self.fixture.user
        self.offering_user = OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="testuser",
        )

        self.offering.project.add_user(self.user, ProjectRole.ADMIN)

        # Create event subscription for the offering user
        from waldur_core.logging import enums as logging_enums
        from waldur_core.logging.tests import factories as logging_factories

        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.OFFERING_USER.value}
            ],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.OFFERING_USER.value,
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_updated_message_sent_on_update(self, mock_publish_messages):
        """Test that update message is sent when OfferingUser is updated."""
        self.offering_user.username = "updateduser"
        self.offering_user.is_restricted = True
        self.offering_user.save()

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        payload = json.loads(message["payload"])

        self.assertEqual(payload["offering_user_uuid"], self.offering_user.uuid.hex)
        self.assertEqual(payload["user_uuid"], self.user.uuid.hex)
        self.assertEqual(payload["username"], "updateduser")
        self.assertEqual(payload["state"], self.offering_user.get_state_display())
        self.assertEqual(payload["action"], "update")
        self.assertIn("username", payload["changed_fields"])
        self.assertIn("is_restricted", payload["changed_fields"])

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_created_message_sent_on_creation(
        self, mock_publish_messages
    ):
        """Test that creation message is sent when OfferingUser is created."""
        # Create a new offering user
        new_offering_user = OfferingUserFactory(
            offering=self.offering,
            user=self.fixture.admin,
            username="newuser",
        )

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        payload = json.loads(message["payload"])
        self.assertEqual(payload["offering_user_uuid"], new_offering_user.uuid.hex)
        self.assertEqual(payload["user_uuid"], self.fixture.admin.uuid.hex)
        self.assertEqual(payload["username"], "newuser")
        self.assertEqual(payload["state"], new_offering_user.get_state_display())
        self.assertEqual(payload["action"], "create")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_updated_message_not_sent_when_no_changes(
        self, mock_publish_messages
    ):
        """Test that update message is NOT sent when no fields changed."""
        self.offering_user.save()

        mock_publish_messages.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_updated_message_sent_for_basic_offering(
        self, mock_publish_messages
    ):
        """Test that update message is sent for basic offerings too."""
        self.offering.type = "basic"
        self.offering.save()

        self.offering_user.username = "updateduser"
        self.offering_user.save()

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        payload = json.loads(message["payload"])

        self.assertEqual(payload["offering_user_uuid"], self.offering_user.uuid.hex)
        self.assertEqual(payload["user_uuid"], self.user.uuid.hex)
        self.assertEqual(payload["username"], "updateduser")
        self.assertEqual(payload["action"], "update")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_deleted_message_sent_on_deletion(
        self, mock_publish_messages
    ):
        """Test that deletion message is sent when OfferingUser is deleted."""
        self.offering_user.delete()

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        payload = json.loads(message["payload"])

        self.assertEqual(payload["offering_user_uuid"], self.offering_user.uuid.hex)
        self.assertEqual(payload["user_uuid"], self.user.uuid.hex)
        self.assertEqual(payload["username"], "testuser")
        self.assertEqual(payload["action"], "delete")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_deleted_message_sent_for_basic_offering(
        self, mock_publish_messages
    ):
        """Test that deletion message is sent for basic offerings too."""
        self.offering.type = "basic"
        self.offering.save()

        self.offering_user.delete()

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        payload = json.loads(message["payload"])

        self.assertEqual(payload["offering_user_uuid"], self.offering_user.uuid.hex)
        self.assertEqual(payload["user_uuid"], self.user.uuid.hex)
        self.assertEqual(payload["username"], "testuser")
        self.assertEqual(payload["action"], "delete")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_state_change_tracking(self, mock_publish_messages):
        """Test that state changes are properly tracked in update messages."""
        self.offering_user.request_deletion()
        self.offering_user.save()

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        payload = json.loads(message["payload"])

        self.assertIn("state", payload["changed_fields"])
        self.assertEqual(payload["state"], self.offering_user.get_state_display())


class OfferingUserComplianceFieldTest(test.APITestCase):
    """Test the has_compliance_checklist field in OfferingUserSerializer."""

    def setUp(self):
        # Create test fixtures following established pattern from ListOfferingUsersTest
        self.fixture = structure_fixtures.ProjectFixture()

        # Create checklist
        self.checklist = ChecklistFactory(
            name="Test Compliance Checklist",
            checklist_type="offering_compliance",
        )

        # Create offerings with proper plugin options (following working pattern)
        self.offering_with_compliance = OfferingFactory(
            shared=True,
            customer=self.fixture.customer,
            compliance_checklist=self.checklist,
        )
        self.offering_with_compliance.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering_with_compliance.save()

        self.offering_without_compliance = OfferingFactory(
            shared=True, customer=self.fixture.customer, compliance_checklist=None
        )
        self.offering_without_compliance.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering_without_compliance.save()

    def test_has_compliance_checklist_field_true_when_compliance_exists(self):
        """Test that has_compliance_checklist returns True when offering user has compliance."""
        # Create test user following the exact working pattern
        sample_user = UserFactory()
        self.fixture.project.add_user(sample_user, ProjectRole.ADMIN)
        OfferingUser.objects.create(
            offering=self.offering_with_compliance,
            user=sample_user,
            username="compliance_user",
        )
        models.UserOfferingConsent.objects.create(
            user=sample_user,
            offering=self.offering_with_compliance,
            version="1.0",
        )

        # Authenticate as the offering user themselves
        self.client.force_authenticate(sample_user)
        response = self.client.get(OfferingUserFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(1, len(response.data))
        self.assertIn("has_compliance_checklist", response.data[0])
        self.assertTrue(response.data[0]["has_compliance_checklist"])

    def test_has_compliance_checklist_field_false_when_no_compliance(self):
        """Test that has_compliance_checklist returns False when offering has no compliance."""
        # Create test user following the exact working pattern
        sample_user = UserFactory()
        self.fixture.project.add_user(sample_user, ProjectRole.ADMIN)
        OfferingUser.objects.create(
            offering=self.offering_without_compliance,
            user=sample_user,
            username="no_compliance_user",
        )
        models.UserOfferingConsent.objects.create(
            user=sample_user,
            offering=self.offering_without_compliance,
            version="1.0",
        )

        # Authenticate as the offering user themselves
        self.client.force_authenticate(sample_user)
        response = self.client.get(OfferingUserFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(1, len(response.data))
        self.assertIn("has_compliance_checklist", response.data[0])
        self.assertFalse(response.data[0]["has_compliance_checklist"])

    def _make_offering_user(self, offering, username):
        sample_user = UserFactory()
        self.fixture.project.add_user(sample_user, ProjectRole.ADMIN)
        offering_user = OfferingUser.objects.create(
            offering=offering, user=sample_user, username=username
        )
        models.UserOfferingConsent.objects.create(
            user=sample_user, offering=offering, version="1.0"
        )
        return offering_user

    def test_annotation_matches_the_unannotated_fallback(self):
        """The Exists() annotation must reproduce the per-row query exactly.

        Covers all three shapes: an offering with a checklist and a
        completion, one with a checklist but no completion, and one with no
        checklist at all.
        """
        self._make_offering_user(self.offering_with_compliance, "with_completion")
        without_completion = self._make_offering_user(
            self.offering_with_compliance, "without_completion"
        )
        checklist_models.ChecklistCompletion.objects.filter(
            scope_object_id=without_completion.id,
            scope_content_type=ContentType.objects.get_for_model(OfferingUser),
        ).delete()
        self._make_offering_user(self.offering_without_compliance, "no_checklist")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(OfferingUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        serializer = serializers.OfferingUserSerializer()
        for payload in response.data:
            # Re-fetch without the annotation so the serializer takes its
            # fallback branch, and compare the two answers.
            unannotated = OfferingUser.objects.get(uuid=payload["uuid"])
            self.assertFalse(hasattr(unannotated, "_compliance_completion_exists"))
            self.assertEqual(
                payload["has_compliance_checklist"],
                serializer.get_has_compliance_checklist(unannotated),
                f"mismatch for {payload['username']}",
            )

    def test_query_count_does_not_grow_with_number_of_offering_users(self):
        self._make_offering_user(self.offering_with_compliance, "user_0")
        self.client.force_authenticate(self.fixture.staff)
        url = OfferingUserFactory.get_list_url()
        self.client.get(url)  # warm ContentType and permission caches

        with django_test.CaptureQueriesContext(connection) as baseline:
            self.client.get(url)

        for index in range(1, 6):
            self._make_offering_user(self.offering_with_compliance, f"user_{index}")

        with self.assertNumQueries(len(baseline)):
            self.client.get(url)


@ddt
class OfferingUserCommentUrlResetTest(test.APITestCase):
    """Test cases for the comment_url field reset bug fix."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=user,
            username="user",
        )
        # Force the state after creation (in case signals override it)
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, offering_user, action):
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        return url + action + "/"

    def test_set_pending_additional_validation_with_empty_comment_url(self):
        """Test that empty string comment_url is properly set."""
        # Set initial comment_url
        self.offering_user.service_provider_comment_url = "https://example.com/initial"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {
            "comment": "Additional validation needed",
            "comment_url": "",  # Empty string should reset the field
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        self.assertEqual(
            self.offering_user.service_provider_comment, "Additional validation needed"
        )
        # Bug fix: empty string should reset the comment_url field
        self.assertEqual(self.offering_user.service_provider_comment_url, "")

    def test_set_pending_additional_validation_with_none_comment_url(self):
        """Test that None comment_url raises validation error."""
        # Set initial comment_url
        self.offering_user.service_provider_comment_url = "https://example.com/initial"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {
            "comment": "Additional validation needed",
            "comment_url": None,  # None should raise validation error
        }

        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("comment_url", response.data)
        self.assertIn("This field may not be null.", response.data["comment_url"])

    def test_set_pending_additional_validation_without_comment_url(self):
        """Test that omitting comment_url leaves field unchanged."""
        # Set initial comment_url
        initial_url = "https://example.com/initial"
        self.offering_user.service_provider_comment_url = initial_url
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {
            "comment": "Additional validation needed"
            # Omit comment_url - should leave field unchanged
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )
        # Field should remain unchanged when not provided
        self.assertEqual(self.offering_user.service_provider_comment_url, initial_url)

    def test_set_pending_account_linking_with_empty_comment_url(self):
        """Test that set_pending_account_linking also handles empty comment_url correctly."""
        # Set state to valid source state for this transition
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.service_provider_comment_url = "https://example.com/initial"
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_account_linking")
        payload = {
            "comment": "Please link your account",
            "comment_url": "",  # Empty string should reset the field
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.state, OfferingUserStates.PENDING_ACCOUNT_LINKING
        )
        # Bug fix: empty string should reset the comment_url field
        self.assertEqual(self.offering_user.service_provider_comment_url, "")

    @data(
        ["", ""],  # Empty string to empty string
        ["https://example.com/test", ""],  # URL to empty string
        ["", "https://example.com/new"],  # Empty string to URL
        ["https://example.com/old", "https://example.com/new"],  # URL to URL
    )
    def test_comment_url_field_transitions(self, test_data):
        """Test various comment_url transitions work correctly."""
        initial_url, target_url = test_data
        self.offering_user.service_provider_comment_url = initial_url
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {"comment": "Test transition", "comment_url": target_url}

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.service_provider_comment_url, target_url)


class OfferingUserProfileCompletenessTest(test.APITestCase):
    """Tests for filtering OfferingUsers by user attribute completeness."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()

        # Create a user with complete profile
        self.complete_user = UserFactory(
            first_name="John",
            last_name="Doe",
            email="john@example.com",
            username="johndoe",
        )
        self.fixture.project.add_user(self.complete_user, ProjectRole.ADMIN)
        self.complete_offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.complete_user,
            username="johndoe",
        )

        # Create a user with incomplete profile (empty email)
        self.incomplete_user = UserFactory(
            first_name="Jane",
            last_name="Doe",
            email="",
            username="janedoe",
        )
        self.fixture.project.add_user(self.incomplete_user, ProjectRole.ADMIN)
        self.incomplete_offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.incomplete_user,
            username="janedoe",
        )

        # Create attribute config requiring email
        self.config = models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_registration_method=False,
        )

    def _list_offering_users(self, user, params=None):
        self.client.force_authenticate(user=user)
        url = OfferingUserFactory.get_list_url()
        return self.client.get(url, params)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_sp_cannot_see_offering_user_with_incomplete_profile(self):
        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertNotIn(str(self.incomplete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_sp_can_see_offering_user_with_complete_profile(self):
        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.complete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_user_always_sees_own_record(self):
        """A user with incomplete profile still sees their own OfferingUser."""
        response = self._list_offering_users(self.incomplete_user)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.incomplete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_staff_sees_all_regardless_of_completeness(self):
        response = self._list_offering_users(self.fixture.staff)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.complete_offering_user.uuid), uuids)
        self.assertIn(str(self.incomplete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=False)
    def test_enforcement_off_shows_all_records(self):
        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.complete_offering_user.uuid), uuids)
        self.assertIn(str(self.incomplete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_no_config_on_offering_means_no_filtering(self):
        """When offering has no OfferingUserAttributeConfig, all users are visible."""
        self.config.delete()
        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.complete_offering_user.uuid), uuids)
        self.assertIn(str(self.incomplete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_json_field_empty_list_is_incomplete(self):
        """Empty affiliations list treated as incomplete."""
        self.config.expose_affiliations = True
        self.config.save()
        # complete_user has affiliations=[] by default
        self.complete_user.affiliations = []
        self.complete_user.save()

        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertNotIn(str(self.complete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_full_name_incomplete_only_when_both_empty(self):
        """full_name is incomplete only when both first_name AND last_name are empty."""
        self.config.expose_email = False
        self.config.save()

        # Only last_name empty - should be complete
        self.complete_user.first_name = "John"
        self.complete_user.last_name = ""
        self.complete_user.save()

        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.complete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_full_name_incomplete_when_both_names_empty(self):
        """full_name is incomplete when both first_name AND last_name are empty."""
        self.config.expose_email = False
        self.config.save()

        self.complete_user.first_name = ""
        self.complete_user.last_name = ""
        self.complete_user.save()

        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertNotIn(str(self.complete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=True)
    def test_multiple_attributes_all_must_be_filled(self):
        """ALL exposed attributes must be filled (AND logic)."""
        self.config.expose_phone_number = True
        self.config.save()

        # email is filled but phone_number is not
        self.complete_user.email = "john@example.com"
        self.complete_user.phone_number = ""
        self.complete_user.save()

        response = self._list_offering_users(self.fixture.owner)
        uuids = {item["uuid"] for item in response.data}
        self.assertNotIn(str(self.complete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=False)
    def test_has_complete_profile_filter_true(self):
        """Explicit filter works independently of global toggle."""
        response = self._list_offering_users(
            self.fixture.owner, {"has_complete_profile": True}
        )
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.complete_offering_user.uuid), uuids)
        self.assertNotIn(str(self.incomplete_offering_user.uuid), uuids)

    @override_config(ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS=False)
    def test_has_complete_profile_filter_false(self):
        """Explicit filter can find incomplete profiles."""
        response = self._list_offering_users(
            self.fixture.owner, {"has_complete_profile": False}
        )
        uuids = {item["uuid"] for item in response.data}
        self.assertNotIn(str(self.complete_offering_user.uuid), uuids)
        self.assertIn(str(self.incomplete_offering_user.uuid), uuids)

    def test_response_includes_is_profile_complete_true(self):
        """Complete user record includes is_profile_complete=True."""
        response = self._list_offering_users(self.fixture.owner)
        record = next(
            item
            for item in response.data
            if item["uuid"] == str(self.complete_offering_user.uuid)
        )
        self.assertTrue(record["is_profile_complete"])
        self.assertEqual(record["missing_profile_attributes"], [])

    def test_response_includes_is_profile_complete_false(self):
        """Incomplete user record includes is_profile_complete=False with missing attrs."""
        response = self._list_offering_users(self.fixture.owner)
        record = next(
            item
            for item in response.data
            if item["uuid"] == str(self.incomplete_offering_user.uuid)
        )
        self.assertFalse(record["is_profile_complete"])
        self.assertIn("email", record["missing_profile_attributes"])

    def test_own_record_shows_missing_attributes(self):
        """User's own record includes missing_profile_attributes for self-service."""
        response = self._list_offering_users(self.incomplete_user)
        record = next(
            item
            for item in response.data
            if item["uuid"] == str(self.incomplete_offering_user.uuid)
        )
        self.assertFalse(record["is_profile_complete"])
        self.assertIn("email", record["missing_profile_attributes"])


class IdentityManagerOfferingUserVisibilityTest(test.APITestCase):
    """Identity managers can list OfferingUsers whose linked user's active_isds
    overlap with the manager's managed_isds, even without direct offering access."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = OfferingFactory(
            customer=self.fixture.customer,
            shared=True,
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
        }
        self.offering.save()

        # A user with active_isds linked to an offering user
        self.target_user = UserFactory(active_isds=["isd:efp", "isd:puhuri"])
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.target_user,
            username="target-user",
        )

        # Identity manager with matching managed_isds but no offering access
        self.identity_manager = UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:efp"],
        )

    def _list_offering_users(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get(OfferingUserFactory.get_list_url())

    def test_identity_manager_can_list_offering_users_with_overlapping_isds(self):
        response = self._list_offering_users(self.identity_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["username"], "target-user")

    def test_identity_manager_cannot_see_users_without_isd_overlap(self):
        non_matching_user = UserFactory(active_isds=["isd:fenix"])
        OfferingUser.objects.create(
            offering=self.offering,
            user=non_matching_user,
            username="fenix-user",
        )
        response = self._list_offering_users(self.identity_manager)
        usernames = [item["username"] for item in response.data]
        self.assertIn("target-user", usernames)
        self.assertNotIn("fenix-user", usernames)

    def test_non_identity_manager_without_access_cannot_list(self):
        regular_user = UserFactory()
        response = self._list_offering_users(regular_user)
        self.assertEqual(len(response.data), 0)

    def test_identity_manager_without_managed_isds_cannot_list(self):
        manager_no_isds = UserFactory(
            is_identity_manager=True,
            managed_isds=[],
        )
        response = self._list_offering_users(manager_no_isds)
        self.assertEqual(len(response.data), 0)


class OfferingManagerOfferingUserVisibilityTest(test.APITestCase):
    """Offering managers can list and manage OfferingUsers on offerings they manage."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = OfferingFactory(
            customer=self.fixture.customer,
            shared=True,
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
        }
        self.offering.save()

        # A regular user linked to this offering
        self.target_user = UserFactory()
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.target_user,
            username="target-user",
        )

        # An offering manager with ONLY offering-scoped role (no customer role)
        self.offering_manager = UserFactory()
        self.offering.add_user(self.offering_manager, OfferingRole.MANAGER)

    def _list_offering_users(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get(OfferingUserFactory.get_list_url())

    def test_offering_manager_can_list_offering_users(self):
        response = self._list_offering_users(self.offering_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["username"], "target-user")

    def test_offering_manager_cannot_see_users_on_other_offerings(self):
        other_offering = OfferingFactory(shared=True)
        other_offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
        }
        other_offering.save()
        other_user = UserFactory()
        OfferingUser.objects.create(
            offering=other_offering,
            user=other_user,
            username="other-user",
        )

        response = self._list_offering_users(self.offering_manager)
        usernames = [item["username"] for item in response.data]
        self.assertIn("target-user", usernames)
        self.assertNotIn("other-user", usernames)


class OfferingUserPartialUpdatePermissionTest(test.APITestCase):
    """partial_update must enforce permission checks, not just queryset visibility."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = OfferingFactory(
            customer=self.fixture.customer,
            shared=True,
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
        }
        self.offering.save()

        self.target_user = UserFactory(active_isds=["isd:efp"])
        self.offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=self.target_user,
            username="target-user",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def _patch_offering_user(self, user, offering_user, data):
        self.client.force_authenticate(user=user)
        url = OfferingUserFactory.get_url(offering_user)
        return self.client.patch(url, data)

    def test_identity_manager_cannot_patch_offering_user(self):
        identity_manager = UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:efp"],
        )
        response = self._patch_offering_user(
            identity_manager, self.offering_user, {"username": "hacked"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, "target-user")

    def test_customer_owner_can_patch_offering_user(self):
        response = self._patch_offering_user(
            self.fixture.owner, self.offering_user, {"username": "updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, "updated")

    def test_offering_user_cannot_patch_own_record(self):
        response = self._patch_offering_user(
            self.target_user, self.offering_user, {"username": "self-updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, "target-user")

    def test_offering_manager_can_patch_offering_user(self):
        offering_manager = UserFactory()
        self.offering.add_user(offering_manager, OfferingRole.MANAGER)
        response = self._patch_offering_user(
            offering_manager, self.offering_user, {"username": "mgr-updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, "mgr-updated")


@ddt
class OfferingUserUpdateRuntimeStateTest(test.APITestCase):
    """Tests for the update_runtime_state action on OfferingUsersViewSet."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = UserFactory()

        self.offering_user = OfferingUser.objects.create(
            offering=self.offering, user=user, username="user"
        )
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.offering,
            version="1.0",
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, offering_user):
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        return url + "update_runtime_state/"

    def test_new_offering_user_has_active_runtime_state_by_default(self):
        """New OfferingUser defaults to ACTIVE runtime state."""
        user = UserFactory()
        offering_user = OfferingUser.objects.create(offering=self.offering, user=user)
        self.assertEqual(offering_user.runtime_state, OfferingUserRuntimeStates.ACTIVE)

    def test_runtime_state_exposed_in_serializer(self):
        """runtime_state field is present in the API response."""
        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": self.offering_user.uuid.hex},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("runtime_state", response.data)
        self.assertEqual(response.data["runtime_state"], "Active")

    @data("staff", "owner", "service_manager")
    def test_authorized_user_can_update_runtime_state(self, user):
        """Owner/manager can set runtime_state to pending account linking."""
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state,
            OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING,
        )

    def test_unauthorized_user_cannot_update_runtime_state(self):
        """Support user (no UPDATE_OFFERING_USER) is denied."""
        self.client.force_authenticate(user=self.fixture.user)
        self.fixture.customer.add_user(self.fixture.user, CustomerRole.SUPPORT)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_can_set_pending_additional_validation(self):
        """Runtime state can be set to PENDING_ADDITIONAL_VALIDATION (TOU not accepted)."""
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ADDITIONAL_VALIDATION},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state,
            OfferingUserRuntimeStates.PENDING_ADDITIONAL_VALIDATION,
        )

    def test_can_update_ok_lifecycle_user_runtime_state(self):
        """Runtime state can be set even when lifecycle state is OK (the backfill case)."""
        self.offering_user.state = OfferingUserStates.OK
        self.offering_user.save(update_fields=["state"])

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state,
            OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING,
        )

    def test_can_set_runtime_state_back_to_active(self):
        """Runtime state can be reset to ACTIVE after a blocker is resolved."""
        self.offering_user.runtime_state = (
            OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING
        )
        self.offering_user.save(update_fields=["runtime_state"])

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.ACTIVE},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state, OfferingUserRuntimeStates.ACTIVE
        )

    def test_cannot_update_runtime_state_for_deleted_offering_user(self):
        """Updating runtime_state is rejected when lifecycle state is DELETED."""
        self.offering_user.state = OfferingUserStates.DELETED
        self.offering_user.save(update_fields=["state"])

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING},
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state, OfferingUserRuntimeStates.ACTIVE
        )

    def test_invalid_runtime_state_value_returns_400(self):
        """Passing an unknown runtime_state value returns HTTP 400."""
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": 999},
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_event_logged_on_runtime_state_update(self):
        """An event is emitted when runtime state is updated."""
        self.client.force_authenticate(user=self.fixture.owner)
        self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING},
        )
        self.assertTrue(
            Event.objects.filter(
                event_type="marketplace_offering_user_updated"
            ).exists()
        )

    def test_can_update_runtime_state_with_comments(self):
        """Runtime state and service provider comments can be set in one request."""
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {
                "runtime_state": OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING,
                "service_provider_comment": "Please link your MyAccessID account",
                "service_provider_comment_url": "https://help.example.com/linking",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state,
            OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING,
        )
        self.assertEqual(
            self.offering_user.service_provider_comment,
            "Please link your MyAccessID account",
        )
        self.assertEqual(
            self.offering_user.service_provider_comment_url,
            "https://help.example.com/linking",
        )

    def test_runtime_state_update_without_comments_leaves_existing_comments(self):
        """Omitting comment fields does not clear existing service provider comments."""
        self.offering_user.service_provider_comment = "Existing comment"
        self.offering_user.service_provider_comment_url = "https://example.com/existing"
        self.offering_user.save(
            update_fields=["service_provider_comment", "service_provider_comment_url"]
        )

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {"runtime_state": OfferingUserRuntimeStates.PENDING_ADDITIONAL_VALIDATION},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state,
            OfferingUserRuntimeStates.PENDING_ADDITIONAL_VALIDATION,
        )
        self.assertEqual(
            self.offering_user.service_provider_comment, "Existing comment"
        )
        self.assertEqual(
            self.offering_user.service_provider_comment_url,
            "https://example.com/existing",
        )

    def test_can_clear_comments_when_updating_runtime_state(self):
        """Blank comment fields clear existing service provider comments."""
        self.offering_user.service_provider_comment = "Old comment"
        self.offering_user.service_provider_comment_url = "https://example.com/old"
        self.offering_user.save(
            update_fields=["service_provider_comment", "service_provider_comment_url"]
        )

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.get_url(self.offering_user),
            {
                "runtime_state": OfferingUserRuntimeStates.ACTIVE,
                "service_provider_comment": "",
                "service_provider_comment_url": "",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering_user.refresh_from_db()
        self.assertEqual(
            self.offering_user.runtime_state, OfferingUserRuntimeStates.ACTIVE
        )
        self.assertEqual(self.offering_user.service_provider_comment, "")
        self.assertEqual(self.offering_user.service_provider_comment_url, "")


@ddt
class OfferingUserRuntimeStateFilterTest(test.APITestCase):
    """Tests for filtering OfferingUsers by runtime_state."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        user_active = UserFactory()
        user_pending = UserFactory()

        self.offering_user_active = OfferingUser.objects.create(
            offering=self.offering,
            user=user_active,
            username="active_user",
            runtime_state=OfferingUserRuntimeStates.ACTIVE,
        )
        self.offering_user_pending = OfferingUser.objects.create(
            offering=self.offering,
            user=user_pending,
            username="pending_user",
            runtime_state=OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING,
        )

    def test_filter_by_runtime_state_active(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"runtime_state": "Active"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [r["username"] for r in response.data]
        self.assertIn("active_user", usernames)
        self.assertNotIn("pending_user", usernames)

    def test_filter_by_runtime_state_pending_account_linking(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"runtime_state": "Pending account linking"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [r["username"] for r in response.data]
        self.assertIn("pending_user", usernames)
        self.assertNotIn("active_user", usernames)

    def test_filter_by_multiple_runtime_states(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            OfferingUserFactory.get_list_url(),
            {"runtime_state": ["Active", "Pending account linking"]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)


class OfferingUserRuntimeStateStompTest(test.APITestCase):
    """Tests that STOMP messages include runtime_state."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = OfferingFactory()
        self.offering.project = self.fixture.project
        self.offering.save()
        self.user = self.fixture.user
        self.offering_user = OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="testuser",
        )
        self.offering.project.add_user(self.user, ProjectRole.ADMIN)

        from waldur_core.logging import enums as logging_enums
        from waldur_core.logging.tests import factories as logging_factories

        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.OFFERING_USER.value}
            ],
        )
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.OFFERING_USER.value,
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_create_message_includes_runtime_state(self, mock_publish):
        """Creation STOMP message includes runtime_state field."""

        new_offering_user = OfferingUserFactory(
            offering=self.offering,
            user=self.fixture.admin,
            username="newuser",
        )
        mock_publish.assert_called_once()
        payload = json.loads(mock_publish.call_args[0][0][0]["payload"])
        self.assertIn("runtime_state", payload)
        self.assertEqual(
            payload["runtime_state"], new_offering_user.get_runtime_state_display()
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_update_message_includes_runtime_state(self, mock_publish):
        """Update STOMP message includes runtime_state and lists it in changed_fields."""

        self.offering_user.runtime_state = (
            OfferingUserRuntimeStates.PENDING_ACCOUNT_LINKING
        )
        self.offering_user.save(update_fields=["runtime_state"])

        mock_publish.assert_called_once()
        payload = json.loads(mock_publish.call_args[0][0][0]["payload"])
        self.assertIn("runtime_state", payload)
        self.assertEqual(payload["runtime_state"], "Pending account linking")
        self.assertIn("runtime_state", payload["changed_fields"])
