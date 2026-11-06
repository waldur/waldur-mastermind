"""Inviting reviewers to a call's reviewer pool.

One path for every invitation sent to a person by e-mail address: the call
manager's own invitation by e-mail, and the invitations made from reviewers
the proposal's evaluators suggested. Each gets the call's response window
and the invitation e-mail.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_mastermind.proposal import models, reviewer_standing, tasks
from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

# Invitations that may be sent again: unanswered, whether or not their
# response window has passed.
RESENDABLE_STATUSES = (
    ReviewerPoolInvitationStatuses.PENDING,
    ReviewerPoolInvitationStatuses.EXPIRED,
)


class AlreadyInPool(Exception):
    """The person already has an entry in the call's reviewer pool."""

    def __init__(self, entry, message):
        super().__init__(message)
        self.entry = entry
        self.message = message


class PoolIndex:
    """The call's pool entries of a set of people, read in one query.

    An entry is matched by reviewer profile, by the invited user and by the
    invited address, so a person invited one way is found when looked up
    another.
    """

    def __init__(self, call, users=(), emails=()):
        users = [user for user in users if user is not None]
        user_ids = {user.pk for user in users}
        emails = {email.lower() for email in emails if email} | {
            user.email.lower() for user in users if user.email
        }
        query = Q()
        if user_ids:
            query |= Q(reviewer__user_id__in=user_ids) | Q(invited_user_id__in=user_ids)
        if emails:
            query |= Q(invited_email_lower__in=emails) | Q(
                reviewer_email_lower__in=emails
            )
        if not query:
            self.entries = []
            return
        self.entries = list(
            models.CallReviewerPool.objects.filter(call=call)
            .alias(
                invited_email_lower=Lower("invited_email"),
                reviewer_email_lower=Lower("reviewer__user__email"),
            )
            .filter(query)
            .select_related("reviewer__user")
            .order_by("pk")
        )

    @staticmethod
    def _matches(entry, user, email):
        invited = (entry.invited_email or "").lower()
        reviewer_user = entry.reviewer.user if entry.reviewer is not None else None
        if user is not None:
            if entry.invited_user_id == user.pk or (
                reviewer_user is not None and reviewer_user.pk == user.pk
            ):
                return True
            if user.email and invited == user.email.lower():
                return True
        if email:
            email = email.lower()
            if invited == email:
                return True
            if reviewer_user is not None and (reviewer_user.email or "").lower() == (
                email
            ):
                return True
        return False

    def find(self, user=None, email=""):
        """The entry of ``user`` or of ``email``, or None."""
        for entry in self.entries:
            if self._matches(entry, user, email):
                return entry
        return None


def find_pool_entry(call, user=None, email=""):
    """The call's pool entry of ``user`` or of ``email``, or None."""
    return PoolIndex(call, [user], [email]).find(user, email)


def resend_invitation(entry):
    """Send a pending or expired pool invitation again.

    It returns to pending with a new expiry date from the call's assignment
    configuration and a new link, and the invitee is e-mailed once the
    transaction commits. Returns the entry as saved.
    """
    with transaction.atomic():
        entry = models.CallReviewerPool.objects.select_for_update().get(pk=entry.pk)
        if entry.invitation_status not in RESENDABLE_STATUSES:
            raise exceptions.ValidationError(
                _("Only pending or expired invitations can be sent again.")
            )
        invitee = entry.reviewer.user if entry.reviewer else entry.invited_user
        if invitee is not None:
            reviewer_standing.ensure_reviewer_eligible(invitee, entry.call)
        entry.invitation_status = ReviewerPoolInvitationStatuses.PENDING
        entry.invitation_expires_at = models.CallReviewerPool.get_invitation_expires_at(
            entry.call
        )
        # A new link replaces the old one, so a copy of an earlier invitation
        # e-mail cannot answer the re-sent invitation.
        entry.invitation_token = models.generate_invitation_token()
        entry.save(
            update_fields=[
                "invitation_status",
                "invitation_expires_at",
                "invitation_token",
                "modified",
            ]
        )
        transaction.on_commit(
            lambda uuid=entry.uuid: tasks.send_reviewer_invitation_email.delay(uuid)
        )
    return entry


def invite_by_email(
    call, email, invited_by, max_assignments=None, invitation_message=""
):
    """Invite the person with ``email`` to the call's reviewer pool.

    The invitation names the user with that address, and their reviewer
    profile, when they exist, and keeps the workload asked of them
    (``max_assignments``; the pool's default when not given); the e-mail,
    carrying the inviter's ``invitation_message``, goes out once the
    transaction commits. Raises ``AlreadyInPool`` when the address, or that reviewer, is
    already invited.
    """
    existing = models.CallReviewerPool.objects.filter(
        call=call, invited_email__iexact=email
    ).first()
    if existing:
        raise AlreadyInPool(existing, _("An invitation for this email already exists."))

    user = get_user_model().objects.filter(email__iexact=email).first()
    reviewer = None
    if user:
        # A known person is held to the operator's standing and the call's
        # reviewer eligibility now; an unknown one when they accept.
        reviewer_standing.ensure_reviewer_eligible(user, call)
        reviewer = models.ReviewerProfile.objects.filter(user=user).first()
        if reviewer:
            existing = models.CallReviewerPool.objects.filter(
                call=call, reviewer=reviewer
            ).first()
            if existing:
                raise AlreadyInPool(
                    existing, _("This reviewer is already in the pool.")
                )

    try:
        with transaction.atomic():
            pool_member = models.CallReviewerPool.objects.create(
                call=call,
                reviewer=reviewer,  # May be None
                invited_email=email,
                invited_user=user,  # May be None
                invited_by=invited_by,
                invitation_status=ReviewerPoolInvitationStatuses.PENDING,
                invitation_expires_at=models.CallReviewerPool.get_invitation_expires_at(
                    call
                ),
                **(
                    {"max_assignments": max_assignments}
                    if max_assignments is not None
                    else {}
                ),
            )
    except IntegrityError:
        # Invited in the meantime, by a concurrent request.
        existing = find_pool_entry(call, user=user, email=email)
        if existing is None:
            raise
        raise AlreadyInPool(existing, _("An invitation for this email already exists."))
    transaction.on_commit(
        lambda: tasks.send_reviewer_invitation_email.delay(
            pool_member.uuid, invitation_message
        )
    )
    return pool_member
