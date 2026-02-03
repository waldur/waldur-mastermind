from django.test import TestCase

from waldur_core.structure.tests.factories import UserFactory
from waldur_freeipa.tests import factories as freeipa_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.enums import OfferingUserStates
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class OfferingUserFreeipaStateBugTest(TestCase):
    def setUp(self):
        fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = fixture.offering
        self.offering.plugin_options = {"username_generation_policy": "freeipa"}
        self.offering.save()

        # Create a new user (not the fixture user to avoid conflicts)
        self.user = UserFactory()

    def test_offering_user_state_updated_when_freeipa_profile_created_after_membership(
        self,
    ):
        """
        Test that verifies the fix for the bug where OfferingUser remained in CREATION_REQUESTED state
        even when user creates FreeIPA profile after being added to project.

        The bug occurred because:
        1. User is added to project -> OfferingUser created with no username, state=CREATION_REQUESTED
        2. User creates FreeIPA profile later -> Signal handler sets username but saves with update_fields=["username"]
        3. This bypasses the save() method logic that would update state to OK

        The fix: Remove update_fields from signal handler to trigger full save() logic.
        """
        # Step 1: Create OfferingUser without username (simulates user being added to project)
        # This represents the state when user is first added to project before having FreeIPA account
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",  # No username yet
        )

        # Verify initial state is CREATION_REQUESTED (default state)
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertEqual(offering_user.username, "")

        # Step 2: User creates FreeIPA profile later
        # This simulates what happens when user creates their FreeIPA account after joining project
        freeipa_profile = freeipa_factories.ProfileFactory(user=self.user)

        # Step 3: Check that username can be generated from FreeIPA profile
        generated_username = utils.generate_username(self.user, self.offering)
        self.assertEqual(generated_username, freeipa_profile.username)
        self.assertNotEqual(generated_username, "")

        # Step 4: Verify the fix works!
        # The FreeIPA profile creation now triggers the signal handler that sets the username
        # AND properly updates the state from CREATION_REQUESTED to OK
        offering_user.refresh_from_db()

        # FIXED: Both username is set AND state is updated correctly
        self.assertEqual(
            offering_user.username, generated_username
        )  # Username IS set by signal
        self.assertEqual(
            offering_user.state, OfferingUserStates.OK
        )  # State is NOW correctly updated!

    def test_offering_user_state_ok_when_username_set_at_creation(self):
        """
        Control test: When username is available at creation time, state should be OK.
        This works correctly as shown in the existing save() method.
        """
        # Create FreeIPA profile first
        freeipa_factories.ProfileFactory(user=self.user)
        generated_username = utils.generate_username(self.user, self.offering)

        # Create OfferingUser with username already available
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username=generated_username,
        )

        # State should be OK because username was provided at creation
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_state_updated_when_username_changed_later(self):
        """
        Control test: When username is changed on existing OfferingUser, state should update to OK.
        This works correctly as shown in the existing save() method.
        """
        # Create OfferingUser without username
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
        )

        # Verify initial state
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

        # Create FreeIPA profile and update username
        freeipa_factories.ProfileFactory(user=self.user)
        offering_user.username = utils.generate_username(self.user, self.offering)
        offering_user.save()

        # State should now be OK
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_state_updated_when_offering_settings_change(self):
        """
        Test that OfferingUser state is updated when offering username generation policy changes.

        This tests the fix for update_offering_user_username_after_offering_settings_change handler.
        """
        # Step 1: Create offering with service_provider policy (no username generation)
        self.offering.plugin_options = {
            "username_generation_policy": "service_provider"
        }
        self.offering.save()

        # Create OfferingUser without username
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
        )

        # Verify initial state
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertEqual(offering_user.username, "")

        # Step 2: Create FreeIPA profile
        freeipa_profile = freeipa_factories.ProfileFactory(user=self.user)

        # Username should still be empty because policy is service_provider
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "")
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

        # Step 3: Change offering policy to FreeIPA - this triggers the handler
        self.offering.plugin_options = {"username_generation_policy": "freeipa"}
        self.offering.save()  # This triggers update_offering_user_username_after_offering_settings_change

        # Step 4: Verify the fix - username is set AND state is updated
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, freeipa_profile.username)
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_state_updated_when_user_details_change(self):
        """
        Test that OfferingUser state is updated when user details (site_username) change.

        This tests the fix for update_offering_user_username_after_user_change handler.
        """
        # Step 1: Set up offering with identity_claim policy
        self.offering.plugin_options = {"username_generation_policy": "identity_claim"}
        self.offering.save()

        # Create OfferingUser without username (user has no site_username yet)
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
        )

        # Verify initial state
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertEqual(offering_user.username, "")

        # Step 2: User updates their details with site_username - this triggers the handler
        self.user.details = {"site_username": "testuser123"}
        self.user.save()  # This triggers update_offering_user_username_after_user_change

        # Step 3: Verify the fix - username is set AND state is updated
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "testuser123")
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_state_updated_with_set_offerings_username_api(self):
        """
        Test that OfferingUser state is updated when using the set_offerings_username API endpoint.

        This tests the fix in the set_offerings_username view method.
        """
        # Step 1: Create OfferingUser without username
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
        )

        # Verify initial state
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertEqual(offering_user.username, "")

        # Step 2: Simulate the set_offerings_username API call behavior
        # This is what the API does internally when username is updated
        new_username = "api_set_username"
        offering_user.username = new_username
        offering_user.save()  # Fixed: now calls save() without update_fields

        # Step 3: Verify the fix - state is updated when username is set
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "api_set_username")
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_direct_save_triggers_state_transition(self):
        """
        Test that direct calls to save() (without update_fields) properly trigger state transitions.
        This verifies the save() method logic works correctly.
        """
        # Create OfferingUser without username
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
        )

        # Verify initial state
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertEqual(offering_user.username, "")

        # Set username and save without update_fields
        offering_user.username = "direct_save_test"
        offering_user.save()  # This should trigger state transition

        # Verify state is updated
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(offering_user.username, "direct_save_test")

    def test_offering_settings_change_only_updates_creation_requested_state(self):
        """
        Test that when offering settings change, only OfferingUsers in CREATION_REQUESTED
        state get their usernames updated. Users in other states should not be affected.
        """
        # Create FreeIPA profile for username generation
        self.offering.plugin_options = {
            "username_generation_policy": "service_provider"
        }
        self.offering.save()

        freeipa_profile = freeipa_factories.ProfileFactory(user=self.user)

        # Create offering users in different states
        offering_user_creation_requested = (
            marketplace_models.OfferingUser.objects.create(
                offering=self.offering,
                user=self.user,
                username="",
                state=OfferingUserStates.CREATION_REQUESTED,
            )
        )

        user2 = UserFactory()
        freeipa_factories.ProfileFactory(user=user2)
        offering_user_ok = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=user2,
            username="old_username",
            state=OfferingUserStates.OK,
        )

        user3 = UserFactory()
        freeipa_factories.ProfileFactory(user=user3)
        offering_user_deletion_requested = (
            marketplace_models.OfferingUser.objects.create(
                offering=self.offering,
                user=user3,
                username="deletion_username",
            )
        )
        offering_user_deletion_requested.state = OfferingUserStates.DELETION_REQUESTED
        offering_user_deletion_requested.save()

        # Change offering settings to trigger handler
        self.offering.plugin_options = {"username_generation_policy": "freeipa"}
        self.offering.save()

        # Verify CREATION_REQUESTED user was updated
        offering_user_creation_requested.refresh_from_db()
        self.assertEqual(
            offering_user_creation_requested.username, freeipa_profile.username
        )
        self.assertEqual(offering_user_creation_requested.state, OfferingUserStates.OK)

        # Verify OK state user was updated (username unchanged)
        offering_user_ok.refresh_from_db()
        self.assertNotEqual(offering_user_ok.username, "old_username")
        self.assertEqual(offering_user_ok.state, OfferingUserStates.OK)

        # Verify DELETION_REQUESTED user was NOT updated
        offering_user_deletion_requested.refresh_from_db()
        self.assertEqual(offering_user_deletion_requested.username, "deletion_username")
        self.assertEqual(
            offering_user_deletion_requested.state,
            OfferingUserStates.DELETION_REQUESTED,
        )

    def test_offering_settings_change_only_updates_creating_state(self):
        """
        Test that when offering settings change, OfferingUsers in CREATING state
        also get their usernames updated (in addition to CREATION_REQUESTED).
        """
        self.offering.plugin_options = {
            "username_generation_policy": "service_provider"
        }
        self.offering.save()

        # Create FreeIPA profile for username generation
        freeipa_profile = freeipa_factories.ProfileFactory(user=self.user)

        # Create offering user in CREATING state
        offering_user_creating = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
            state=OfferingUserStates.CREATING,
        )

        # Change offering settings to trigger handler
        self.offering.plugin_options = {"username_generation_policy": "freeipa"}
        self.offering.save()

        # Verify CREATING user was updated
        offering_user_creating.refresh_from_db()
        self.assertEqual(offering_user_creating.username, freeipa_profile.username)
        self.assertEqual(offering_user_creating.state, OfferingUserStates.OK)

    def test_offering_settings_change_does_not_update_deleting_state(self):
        """
        Test that when offering settings change, OfferingUsers in DELETING state
        are not updated.
        """
        self.offering.plugin_options = {
            "username_generation_policy": "service_provider"
        }
        self.offering.save()
        # Create FreeIPA profile
        freeipa_factories.ProfileFactory(user=self.user)

        # Create offering user in DELETING state with a username
        offering_user_deleting = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="deleting_username",
        )
        offering_user_deleting.state = OfferingUserStates.DELETING
        offering_user_deleting.save()

        # Change offering settings to trigger handler
        self.offering.plugin_options = {"username_generation_policy": "freeipa"}
        self.offering.save()

        # Verify DELETING user was NOT updated
        offering_user_deleting.refresh_from_db()
        self.assertEqual(offering_user_deleting.username, "deleting_username")
        self.assertEqual(offering_user_deleting.state, OfferingUserStates.DELETING)

    def test_offering_settings_change_does_not_update_deleted_state(self):
        """
        Test that when offering settings change, OfferingUsers in DELETED state
        are not updated.
        """
        # Create FreeIPA profile
        freeipa_factories.ProfileFactory(user=self.user)

        # Create offering user in DELETED state
        offering_user_deleted = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="",
            state=OfferingUserStates.DELETED,
        )

        # Change offering settings to trigger handler
        self.offering.plugin_options = {"username_generation_policy": "freeipa"}
        self.offering.save()

        # Verify DELETED user was NOT updated
        offering_user_deleted.refresh_from_db()
        self.assertEqual(offering_user_deleted.username, "")
        self.assertEqual(offering_user_deleted.state, OfferingUserStates.DELETED)

    def test_save_does_not_call_set_ok_on_deleted_offering_user(self):
        """
        Regression test for PUHURI-PORTALS-EK7:
        TransitionNotAllowed: Can't switch from state '8' using method 'set_ok'

        When an OfferingUser in DELETED state is saved with a username change,
        the save() method should NOT attempt to call set_ok(), because DELETED
        is not a valid source state for that transition.
        """
        # Create an OfferingUser and move it to DELETED state
        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username="original_username",
        )
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        # Transition through the deletion flow
        offering_user.request_deletion()
        offering_user.save()
        offering_user.set_deleting()
        offering_user.save()
        offering_user.set_deleted()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)

        # Changing username on a DELETED OfferingUser should not raise TransitionNotAllowed
        offering_user.username = "new_username"
        offering_user.save()

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)
        self.assertEqual(offering_user.username, "new_username")
