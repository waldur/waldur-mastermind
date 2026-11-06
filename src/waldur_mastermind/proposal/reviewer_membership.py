"""One list of a call's reviewers.

A reviewer joins a call in one of two ways: by accepting an invitation to its
reviewer pool, or by being given its reviewer role (CALL.REVIEWER) on the
Team tab. Everything that picks reviewers -- suggestions, affinities,
generated assignments, bids, staffing counts -- reads the pool, while direct
review grants, step notifications and the "reviewer" responsible role read the
role. The two are kept in step here, so a reviewer is never in one and not
the other:

- an accepted pool entry gives its reviewer the role;
- a granted role puts its holder in the pool as accepted;
- a revoked role marks their pool entry declined (kept, with its history,
  not deleted: assignment batches hang off it).

Each direction checks before it writes, so neither sets the other off again.
"""

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import UserRole
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

GRANTED_REASON = "Given the call's reviewer role."
REVOKED_REASON = "The call's reviewer role was revoked."


def _is_call_reviewer_role(role) -> bool:
    return role.name == RoleEnum.CALL_REVIEWER or bool(
        role.template_id and role.template.name == RoleEnum.CALL_REVIEWER
    )


def _entry_user(entry):
    return entry.reviewer.user if entry.reviewer_id else entry.invited_user


def _entry_of(call, user):
    """``user``'s entry in ``call``'s pool: by their profile, as invitee, or
    else an invitation sent to their address that no account has taken up.

    The last matters because the pool allows one entry per address: an entry
    created beside such an invitation would break ``unique_call_invited_email``.
    """
    pool = models.CallReviewerPool.objects.filter(call=call).order_by("pk")
    return (
        pool.filter(Q(reviewer__user=user) | Q(invited_user=user)).first()
        or pool.filter(models.CallReviewerPool.addressed_to(user)).first()
    )


def _link(entry, user):
    """Bind an invitation sent to ``user``'s address to their account, as
    accepting it does: to their profile when they have one, and to them."""
    if entry.reviewer_id:
        return
    profile = models.ReviewerProfile.objects.filter(user=user).first()
    if (
        profile is not None
        and not models.CallReviewerPool.objects.filter(
            call_id=entry.call_id, reviewer=profile
        )
        .exclude(pk=entry.pk)
        .exists()
    ):
        entry.reviewer = profile
    entry.invited_user = user


def grant_role_on_acceptance(sender, instance, created=False, **kwargs):
    """An accepted pool entry gives its reviewer the call's reviewer role."""
    if instance.invitation_status != ReviewerPoolInvitationStatuses.ACCEPTED:
        return
    if not created and not instance.tracker.has_changed("invitation_status"):
        return
    user = _entry_user(instance)
    if user is None or instance.call.has_user(user, CallRole.REVIEWER):
        return
    instance.call.add_user_or_skip(
        user,
        CallRole.REVIEWER,
        created_by=instance.overridden_by or instance.invited_by,
        reason="Accepted the call's reviewer pool invitation.",
    )


def join_pool_on_role_grant(sender, instance, current_user=None, **kwargs):
    """Whoever is given the call's reviewer role is in its pool, accepted."""
    if not isinstance(instance.scope, models.Call):
        return
    if not _is_call_reviewer_role(instance.role):
        return
    call, user = instance.scope, instance.user
    now = timezone.now()
    with transaction.atomic():
        entry = _entry_of(call, user)
        if entry is not None:
            if entry.invitation_status == ReviewerPoolInvitationStatuses.ACCEPTED:
                return
            _link(entry, user)
            entry.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
            entry.response_date = now
            entry.override_reason = GRANTED_REASON
            entry.overridden_by = current_user
            entry.overridden_at = now
            entry.save()
            return
        profile = models.ReviewerProfile.objects.filter(user=user).first()
        if profile is None and not user.email:
            # A pool entry names a profile or an address; with neither there
            # is nothing to list, and the role alone still works.
            return
        models.CallReviewerPool.objects.create(
            call=call,
            reviewer=profile,
            invited_user=None if profile else user,
            invited_email="" if profile else (user.email or ""),
            invited_by=current_user,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            response_date=now,
            override_reason=GRANTED_REASON,
            overridden_by=current_user,
            overridden_at=now,
        )


def leave_pool_on_role_revoke(sender, instance, **kwargs):
    """Whoever loses the call's reviewer role is no longer an accepted member
    of its pool: their entry is marked declined, not deleted."""
    if not isinstance(instance.scope, models.Call):
        return
    if not _is_call_reviewer_role(instance.role):
        return
    entry = _entry_of(instance.scope, instance.user)
    if (
        entry is None
        or entry.invitation_status != ReviewerPoolInvitationStatuses.ACCEPTED
    ):
        return
    entry.invitation_status = ReviewerPoolInvitationStatuses.DECLINED
    entry.decline_reason = REVOKED_REASON
    entry.response_date = timezone.now()
    entry.save()


def sync_call_reviewers(calls):
    """Bring each call's pool and reviewer role into step, for data written
    around the signals above (a demo preset's rows, say): every holder of the
    role joins the pool as accepted, and every accepted member gets the role.
    """
    for call in calls:
        grants = UserRole.objects.filter(
            content_type=ContentType.objects.get_for_model(models.Call),
            object_id=call.pk,
            is_active=True,
        ).select_related("role__template", "user")
        for grant in grants:
            join_pool_on_role_grant(None, grant)
        for entry in models.CallReviewerPool.objects.filter(
            call=call, invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED
        ):
            grant_role_on_acceptance(None, entry, created=True)
