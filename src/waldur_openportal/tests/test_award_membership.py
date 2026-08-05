import json
from unittest import mock

import openportal
from constance.test.unittest import override_config
from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_openportal.board import OpenPortalBoard

IDENTIFIER = "someproject.someportal"
DESTINATION = "someportal.somebridge.someoffering"


def build_details(membership_control=None, members=None):
    """
    Build the AwardDetails that a remote portal would send to us.
    """
    payload = {}

    if membership_control is not None:
        payload["membership_control"] = membership_control

    if members is not None:
        payload["members"] = members

    return openportal.AwardDetails(json.dumps(payload))


class MembershipSyncFixture:
    """
    Stands up update_award with everything before the membership block mocked
    out: which members are added and removed is the point, not how the
    ManagedProject is resolved.
    """

    def setUp(self):
        config_patcher = mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        self.board = OpenPortalBoard(DESTINATION)

        self.project = mock.Mock(
            is_removed=False,
            is_expired=False,
            is_in_grace_period=False,
            name="Some project",
            description="",
            start_date=None,
            end_date=None,
        )

        self.project_template = mock.Mock()
        self.project_template.action_needs_approval.return_value = False
        # Every remote role maps onto the same local role for these tests.
        self.local_role = mock.Mock()
        self.local_role.name = "PROJECT.ADMIN"
        self.project_template.get_local_role_for.return_value = self.local_role

        self.managed_project = mock.Mock(
            project=self.project,
            local_identifier="someproject",
        )
        self.managed_project.get_project_template.return_value = self.project_template
        self.managed_project.is_approved.return_value = True
        self.managed_project.is_pending.return_value = False
        self.managed_project.is_canceled.return_value = False
        self.managed_project.is_rejected.return_value = False
        # merge_details is exercised separately; here it is a pass-through so
        # the assertions speak about the details the remote portal sent.
        self.managed_project.merge_details.side_effect = lambda details: details

        objects_patcher = mock.patch(
            "waldur_openportal.board.models.ManagedProject.objects"
        )
        managed_objects = objects_patcher.start()
        managed_objects.get.return_value = self.managed_project
        self.addCleanup(objects_patcher.stop)

        utils_patcher = mock.patch("waldur_openportal.board.utils")
        self.utils = utils_patcher.start()
        self.addCleanup(utils_patcher.stop)

        self.utils.get_project_members.return_value = {
            "alice@example.com": "PROJECT.ADMIN",
            "bob@example.com": "PROJECT.ADMIN",
        }

    def update(self, details):
        return self.board.update_award(
            openportal.ProjectIdentifier(IDENTIFIER), details
        )


class MembershipSyncTest(MembershipSyncFixture, TestCase):
    """Which members update_award adds and removes."""

    def test_absent_member_list_does_not_remove_anybody(self):
        """
        members=None means "do not manage membership".  It must not be read
        as an empty authoritative list, which would revoke every member.
        """
        details = build_details(membership_control="locked", members=None)

        self.assertIsNone(details.members)
        self.assertFalse(details.can_change_membership())

        self.update(details)

        self.utils.remove_project_member.assert_not_called()

    def test_absent_member_list_does_not_remove_anybody_when_roles_only(self):
        details = build_details(membership_control="roles_only", members=None)

        self.assertFalse(details.can_change_membership())

        self.update(details)

        self.utils.remove_project_member.assert_not_called()

    def test_authoritative_member_list_removes_absent_members(self):
        """
        When the sender owns membership and does send a list, members missing
        from that list are revoked.
        """
        details = build_details(
            membership_control="locked",
            members={"alice@example.com": "admin"},
        )

        self.update(details)

        self.utils.remove_project_member.assert_called_once_with(
            self.project, "bob@example.com"
        )

    def test_empty_member_list_removes_every_member(self):
        """
        An explicitly empty list is different from None: it does mean
        "nobody should be a member".
        """
        details = build_details(membership_control="locked", members={})

        self.assertIsNotNone(details.members)

        self.update(details)

        self.assertEqual(
            sorted(
                call.args[1] for call in self.utils.remove_project_member.call_args_list
            ),
            ["alice@example.com", "bob@example.com"],
        )

    def test_open_membership_never_removes_members(self):
        details = build_details(
            membership_control="open",
            members={"alice@example.com": "admin"},
        )

        self.assertTrue(details.can_change_membership())

        self.update(details)

        self.utils.remove_project_member.assert_not_called()

    def test_incoming_member_address_is_matched_case_insensitively(self):
        """
        get_project_members() keys on the lowercased address, so a mixed-case
        incoming address must not look like a brand new member.
        """
        details = build_details(
            membership_control="locked",
            members={"Alice@Example.com": "admin", "bob@example.com": "admin"},
        )

        self.update(details)

        self.utils.set_project_member_role.assert_not_called()
        self.utils.remove_project_member.assert_not_called()

    @override_config(OPENPORTAL_MEMBERSHIP_SYNC_MODE="direct")
    def test_new_member_is_added_directly_in_direct_mode(self):
        details = build_details(
            membership_control="open",
            members={
                "alice@example.com": "admin",
                "bob@example.com": "admin",
                "carol@example.com": "admin",
            },
        )

        self.update(details)

        self.utils.set_project_member_role.assert_called_once_with(
            project=self.project,
            email="carol@example.com",
            role=self.local_role,
            is_existing_member=False,
        )


class MembershipSyncModeTest(MembershipSyncFixture, TestCase):
    """
    How a new member is added is a site policy, chosen by
    OPENPORTAL_MEMBERSHIP_SYNC_MODE. Only the "not a member yet" branch differs:
    a role correction or a removal applies to someone who is already there, so
    there is nothing to consent to.
    """

    def new_member(self):
        return build_details(
            membership_control="open",
            members={"alice@example.com": "admin", "carol@example.com": "admin"},
        )

    @override_config(OPENPORTAL_MEMBERSHIP_SYNC_MODE="invitation")
    def test_invitation_mode_invites_rather_than_granting(self):
        self.update(self.new_member())

        self.utils.invite_user_to_project.assert_called_once_with(
            project=self.project,
            email="carol@example.com",
            role=self.local_role,
            send_email=True,
        )
        self.utils.set_project_member_role.assert_not_called()

    @override_config(OPENPORTAL_MEMBERSHIP_SYNC_MODE="direct")
    def test_direct_mode_grants_rather_than_inviting(self):
        self.update(self.new_member())

        self.utils.set_project_member_role.assert_called_once_with(
            project=self.project,
            email="carol@example.com",
            role=self.local_role,
            is_existing_member=False,
        )
        self.utils.invite_user_to_project.assert_not_called()

    def test_invitation_is_the_default(self):
        """An unconfigured deployment does not silently create accounts."""
        self.update(self.new_member())

        self.utils.invite_user_to_project.assert_called_once()
        self.utils.set_project_member_role.assert_not_called()

    @override_config(OPENPORTAL_MEMBERSHIP_SYNC_MODE="invitation")
    def test_mode_does_not_affect_role_corrections(self):
        """
        Correcting the role of an existing member is not an addition, so it
        goes through the direct path in either mode.
        """
        details = build_details(
            membership_control="locked",
            members={"alice@example.com": "different-role", "bob@example.com": "admin"},
        )
        other_role = mock.Mock()
        other_role.name = "PROJECT.MEMBER"
        self.project_template.get_local_role_for.return_value = other_role

        self.update(details)

        self.utils.set_project_member_role.assert_any_call(
            project=self.project,
            email="alice@example.com",
            role=other_role,
            is_existing_member=True,
        )
        self.utils.invite_user_to_project.assert_not_called()

    @override_config(OPENPORTAL_MEMBERSHIP_SYNC_MODE="invitation")
    def test_mode_does_not_affect_removals(self):
        details = build_details(
            membership_control="locked", members={"alice@example.com": "admin"}
        )

        self.update(details)

        self.utils.remove_project_member.assert_called_once_with(
            self.project, "bob@example.com"
        )

    @override_config(OPENPORTAL_MEMBERSHIP_SYNC_MODE="direct")
    def test_a_member_failing_the_role_rules_does_not_abandon_the_rest(self):
        """
        set_project_member_role validates the grant, so one member can be
        refused - a second project manager, say. The award still has to apply
        to everyone else.
        """
        details = build_details(
            membership_control="locked",
            members={
                "alice@example.com": "admin",
                "carol@example.com": "admin",
                "dave@example.com": "admin",
            },
        )
        self.utils.set_project_member_role.side_effect = [
            ValidationError("Project already has a manager"),
            None,
        ]

        self.update(details)

        added = [
            call.kwargs["email"]
            for call in self.utils.set_project_member_role.call_args_list
        ]
        self.assertEqual(sorted(added), ["carol@example.com", "dave@example.com"])
        # bob is still absent from the authoritative list, so he still goes.
        self.utils.remove_project_member.assert_called_once_with(
            self.project, "bob@example.com"
        )
