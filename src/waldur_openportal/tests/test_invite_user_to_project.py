"""invite_user_to_project must not mint duplicate PENDING invitations.

Openportal sync calls this every tick. Without a duplicate guard a single
unaccepted invite turns into N rows over time.
"""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import Invitation
from waldur_openportal import utils


class InviteUserToProjectTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.role = ProjectRole.MEMBER
        self.email = "alice@example.com"

    def _pending(self):
        project_ct = ContentType.objects.get_for_model(self.project)
        return Invitation.objects.filter(
            email__iexact=self.email,
            role=self.role,
            content_type=project_ct,
            object_id=self.project.id,
            state=InvitationState.PENDING,
        )

    def test_creates_invitation_when_none_exists(self):
        with mock.patch(
            "waldur_openportal.utils.user_tasks.process_invitation.delay"
        ) as mock_send:
            utils.invite_user_to_project(
                self.project, self.email, self.role, send_email=False
            )

        self.assertEqual(self._pending().count(), 1)
        mock_send.assert_not_called()

    def test_repeated_calls_do_not_create_duplicates(self):
        utils.invite_user_to_project(
            self.project, self.email, self.role, send_email=False
        )
        utils.invite_user_to_project(
            self.project, self.email, self.role, send_email=False
        )
        utils.invite_user_to_project(
            self.project, self.email, self.role, send_email=False
        )
        self.assertEqual(self._pending().count(), 1)

    def test_case_insensitive_email_match(self):
        utils.invite_user_to_project(
            self.project, "Alice@Example.com", self.role, send_email=False
        )
        utils.invite_user_to_project(
            self.project, "alice@example.com", self.role, send_email=False
        )
        # Either casing — still one row
        project_ct = ContentType.objects.get_for_model(self.project)
        self.assertEqual(
            Invitation.objects.filter(
                role=self.role,
                content_type=project_ct,
                object_id=self.project.id,
                state=InvitationState.PENDING,
            ).count(),
            1,
        )

    def test_different_project_still_creates_invitation(self):
        utils.invite_user_to_project(
            self.project, self.email, self.role, send_email=False
        )
        other_project = structure_factories.ProjectFactory()
        utils.invite_user_to_project(
            other_project, self.email, self.role, send_email=False
        )
        # Same email+role, different scope — both allowed
        self.assertEqual(
            Invitation.objects.filter(
                email__iexact=self.email,
                role=self.role,
                state=InvitationState.PENDING,
            ).count(),
            2,
        )

    def test_skips_when_existing_invitation_is_pending_project(self):
        Invitation.objects.create(
            scope=self.project,
            email=self.email,
            role=self.role,
            customer=self.project.customer,
            created_by=utils.get_openportal_robot(),
            state=InvitationState.PENDING_PROJECT,
        )
        utils.invite_user_to_project(
            self.project, self.email, self.role, send_email=False
        )
        # Existing PENDING_PROJECT is honored; no fresh PENDING row added.
        project_ct = ContentType.objects.get_for_model(self.project)
        self.assertEqual(
            Invitation.objects.filter(
                email__iexact=self.email,
                role=self.role,
                content_type=project_ct,
                object_id=self.project.id,
                state__in=[InvitationState.PENDING, InvitationState.PENDING_PROJECT],
            ).count(),
            1,
        )

    def test_accepted_invitation_does_not_block_new_one(self):
        # The duplicate guard only matches pending states; an accepted
        # invitation must not block re-inviting (e.g. after offboarding).
        Invitation.objects.create(
            scope=self.project,
            email=self.email,
            role=self.role,
            customer=self.project.customer,
            created_by=utils.get_openportal_robot(),
            state=InvitationState.ACCEPTED,
        )
        utils.invite_user_to_project(
            self.project, self.email, self.role, send_email=False
        )
        self.assertEqual(self._pending().count(), 1)
