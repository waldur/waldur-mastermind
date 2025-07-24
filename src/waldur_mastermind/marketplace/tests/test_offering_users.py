from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.enums import OfferingUserStates, ResourceStates
from waldur_mastermind.marketplace.models import OfferingUser

from . import factories, fixtures


@ddt
class ListOfferingUsersTest(test.APITransactionTestCase):
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
        user2 = UserFactory()
        offering2 = factories.OfferingFactory(shared=True)
        self.fixture.project.add_user(user, ProjectRole.MANAGER)
        OfferingUser.objects.create(offering=offering2, user=user2, username="user2")

    def list_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.get(reverse("marketplace-offering-user-list"))

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

        self.client.force_authenticate(sample_user)
        response = self.client.get(reverse("marketplace-offering-user-list"))

        self.assertEqual(1, len(response.data))
        self.assertEqual("user3", response.data[0]["username"])

    def test_user_can_filter_offering_users(self):
        offering_user1 = OfferingUser.objects.get(username="user")
        offering_user1.save()

        self.client.force_login(self.fixture.staff)

        response = self.client.get(
            reverse("marketplace-offering-user-list"),
            {"provider_uuid": self.offering.customer.uuid.hex},
        )
        self.assertEqual(1, len(response.data))
        self.assertEqual("user", response.data[0]["username"])

    def test_user_can_filter_by_user_username(self):
        offering_user = OfferingUser.objects.get(username="user")
        user = offering_user.user
        user.username = "UserName1"
        user.save()

        self.client.force_login(self.fixture.staff)

        response = self.client.get(
            reverse("marketplace-offering-user-list"), {"user_username": "username1"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(offering_user.user.get_username(), user.username)


@ddt
class CreateOfferingUsersTest(test.APITransactionTestCase):
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
        return self.client.post(reverse("marketplace-offering-user-list"), payload)

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
        response = self.client.post(reverse("marketplace-offering-user-list"), payload)
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
        response = self.client.post(reverse("marketplace-offering-user-list"), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_user_with_missing_fields(self):
        """Should fail when neither URL nor UUID fields are provided."""
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {"username": "testuser"}
        response = self.client.post(reverse("marketplace-offering-user-list"), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class ListUsersTest(test.APITransactionTestCase):
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
class OfferingUsersUpdateTest(test.APITransactionTestCase):
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
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)

    def get_url(self, offering_user, action=None):
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        return url if action is None else url + action + "/"

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

    @data("customer_support", "service_manager")
    def test_unauthorized_user_can_not_update_offering_user(self, user):
        response = self.update_offering_user(user, self.offering_user)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class OfferingUsersDeleteTest(test.APITransactionTestCase):
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
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING_USER)

    def get_url(self, offering_user):
        return "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )

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


class OfferingUsersHandlerTest(test.APITransactionTestCase):
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


@ddt
class OferingUserRestrictedUpdateTest(test.APITransactionTestCase):
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
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "owner", "service_manager")
    def test_owner_manager_can_update_offering_user_restriction(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.update_restriction_status(self.offering_user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering_user.refresh_from_db()
        self.assertTrue(self.offering_user.is_restricted)
        self.assertTrue(
            Event.objects.filter(
                event_type="marketplace_offering_user_restriction_updated"
            ).exists()
        )


@ddt
class OfferingUserStateTransitionTest(test.APITransactionTestCase):
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

    def test_set_pending_additional_validation_transition(self):
        """Test transition to PENDING_ADDITIONAL_VALIDATION state with comment."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        payload = {"comment": "Additional documents required"}
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
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

        self.assertEqual(response.status_code, status.HTTP_200_OK)
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

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.state, OfferingUserStates.OK)
        self.assertEqual(self.offering_user.service_provider_comment, "")

    def test_state_transition_without_comment(self):
        """Test state transitions work without providing comment."""
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

        self.client.force_authenticate(user=self.fixture.owner)
        url = self.get_url(self.offering_user, "set_pending_additional_validation")
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
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

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

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

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("state", response.data)
        self.assertIn("service_provider_comment", response.data)
        self.assertEqual(response.data["state"], "Pending additional validation")
        self.assertEqual(response.data["service_provider_comment"], "Test comment")


@ddt
class OfferingUserBackwardCompatibilityTest(test.APITransactionTestCase):
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

    def test_create_offering_user_with_username_sets_ok_state(self):
        """Test that creating OfferingUser with username automatically sets state to OK."""
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {
            "offering_uuid": self.offering.uuid.hex,
            "user_uuid": self.fixture.user.uuid.hex,
            "username": "testuser",
        }
        response = self.client.post(reverse("marketplace-offering-user-list"), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
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
        response = self.client.post(reverse("marketplace-offering-user-list"), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        offering_user = OfferingUser.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_update_offering_user_with_username_sets_ok_state(self):
        """Test that updating OfferingUser with username automatically sets state to OK."""
        user = UserFactory()
        offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=user,
            state=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        )

        self.client.force_authenticate(user=self.fixture.owner)
        url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {"username": "updated_username"}
        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(offering_user.username, "updated_username")

    def test_update_offering_user_without_username_preserves_state(self):
        """Test that updating other fields doesn't change state."""
        user = UserFactory()
        offering_user = OfferingUser.objects.create(
            offering=self.offering,
            user=user,
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

        self.assertEqual(response.status_code, status.HTTP_200_OK)
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


class SetOfferingsUsernameBackwardCompatibilityTest(test.APITransactionTestCase):
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

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering_user.refresh_from_db()
        self.assertEqual(
            offering_user.state, OfferingUserStates.PENDING_ADDITIONAL_VALIDATION
        )  # State unchanged


@ddt
class OfferingUserStateFilterTest(test.APITransactionTestCase):
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

    def test_filter_by_single_state(self):
        """Test filtering by a single state value."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            reverse("marketplace-offering-user-list"),
            {"state": "Requested"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering_user1.uuid.hex)

    def test_filter_by_multiple_states(self):
        """Test filtering by multiple state values."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            reverse("marketplace-offering-user-list"),
            {"state": ["Requested", "OK"]},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
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
            reverse("marketplace-offering-user-list"),
            {"state": "Pending additional validation"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering_user2.uuid.hex)

    def test_filter_by_nonexistent_state(self):
        """Test filtering by a state that doesn't exist returns validation error."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            reverse("marketplace-offering-user-list"),
            {"state": "NonexistentState"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("state", response.data)

    def test_filter_combines_with_other_filters(self):
        """Test that state filter can be combined with other filters."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            reverse("marketplace-offering-user-list"),
            {
                "state": ["Requested", "OK"],
                "offering_uuid": self.offering.uuid.hex,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # All results should be from the same offering
        for item in response.data:
            self.assertEqual(item["offering_uuid"], self.offering.uuid.hex)

    def test_no_state_filter_returns_all_users(self):
        """Test that without state filter, all offering users are returned."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(reverse("marketplace-offering-user-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)
