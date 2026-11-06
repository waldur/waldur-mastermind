"""The operator's reviewer registry: a reviewer's standing, and the approval
of profile publication when the operator requires it.

Every change goes through here, so each is recorded on the profile and logged
as an event (``EventGroup.REVIEW``) with who made it and why.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.configuration import config
from waldur_core.core.enums import ReviewStates
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import enums, models


def _name(user):
    return user.full_name or user.username if user else ""


def _emit(profile, event_type, message, **context):
    event_logger.emit(
        message,
        event_type=event_type,
        event_context={
            "reviewer_uuid": profile.uuid.hex,
            "reviewer_user_uuid": profile.user.uuid.hex,
            **context,
        },
        scopes=[profile.user],
    )


@transaction.atomic
def set_standing(profile, standing, reason, acting_user):
    """Set ``profile``'s standing, recording who set it, when and why.

    Nothing is deleted: the profile, its conflicts of interest, disclosures,
    pool memberships and reviews all stay. A non-active reviewer is only no
    longer selected for, invited to or offered new review work.
    """
    if standing not in dict(enums.ReviewerStandings.CHOICES):
        raise ValidationError({"standing": _("Unknown standing.")})
    reason = (reason or "").strip()
    if standing != enums.ReviewerStandings.ACTIVE and not reason:
        raise ValidationError(
            {"reason": _("Say why the reviewer is suspended or blocked.")}
        )
    profile = models.ReviewerProfile.objects.select_for_update().get(pk=profile.pk)
    previous = profile.standing
    profile.standing = standing
    profile.standing_reason = reason
    profile.standing_changed_by = acting_user
    profile.standing_changed_at = timezone.now()
    profile.save(
        update_fields=[
            "standing",
            "standing_reason",
            "standing_changed_by",
            "standing_changed_at",
        ]
    )
    _emit(
        profile,
        EventType.REVIEWER_STANDING_CHANGED,
        "Standing of reviewer %(reviewer)s changed from %(previous)s to "
        "%(standing)s by %(user)s%(reason)s"
        % {
            "reviewer": _name(profile.user),
            "previous": previous,
            "standing": standing,
            "user": _name(acting_user),
            "reason": f": {reason}" if reason else ".",
        },
        previous_standing=previous,
        standing=standing,
        reason=reason,
        acting_user_uuid=acting_user.uuid.hex if acting_user else None,
    )
    return profile


def pending_publication_request(profile):
    return profile.publication_requests.filter(state=ReviewStates.PENDING).first()


@transaction.atomic
def publish(profile, user):
    """Publish ``profile``, or ask the operator to, when they approve
    publication. Returns the pending request, or None once published."""
    pending = pending_publication_request(profile)
    if config.REVIEWER_PUBLICATION_REQUIRES_APPROVAL:
        if pending is not None:
            return pending
        request = models.ReviewerPublicationRequest.objects.create(
            profile=profile, created_by=user, state=ReviewStates.PENDING
        )
        _emit(
            profile,
            EventType.REVIEWER_PUBLICATION_REQUESTED,
            "Reviewer %(reviewer)s asked to publish their profile."
            % {"reviewer": _name(profile.user)},
        )
        return request
    # Approval no longer required: a request left from before is overtaken.
    if pending is not None:
        pending.cancel()
    profile.is_published = True
    profile.published_at = timezone.now()
    profile.save(update_fields=["is_published", "published_at"])
    return None


@transaction.atomic
def withdraw_publication_request(profile):
    pending = pending_publication_request(profile)
    if pending is not None:
        pending.cancel()
    return pending


@transaction.atomic
def decide_publication(profile, approve, acting_user, comment=""):
    """Approve or reject ``profile``'s pending publication request."""
    request = (
        models.ReviewerPublicationRequest.objects.select_for_update()
        .filter(profile=profile, state=ReviewStates.PENDING)
        .first()
    )
    if request is None:
        raise ValidationError(
            _("This reviewer has no publication request awaiting a decision.")
        )
    if approve:
        request.approve(acting_user, comment)
        event_type = EventType.REVIEWER_PUBLICATION_APPROVED
        verb = "approved"
    else:
        request.reject(acting_user, comment)
        event_type = EventType.REVIEWER_PUBLICATION_REJECTED
        verb = "rejected"
    _emit(
        profile,
        event_type,
        "Publication of reviewer %(reviewer)s's profile %(verb)s by %(user)s%(comment)s"
        % {
            "reviewer": _name(profile.user),
            "verb": verb,
            "user": _name(acting_user),
            "comment": f": {comment}" if comment else ".",
        },
        comment=comment or "",
        acting_user_uuid=acting_user.uuid.hex,
    )
    return request
