"""Activity history for demo preset proposals.

A proposal's Activity card reads its event feed: created by the applicant,
its team joined, submitted. A preset authors proposals and their team roles
directly, without the requests that log those events, so every demo
proposal had an empty Activity card -- a state production never reaches.

This logs what the proposal's state implies, for every proposal the preset
left without it, as its applicant did it and dated when it happened:

* the proposal created, when it was;
* each member of its team added, when they were;
* a proposal past its draft submitted, when it was -- or shortly after it
  was created, where the preset gives no time;
* a submitted review dated when its submission was logged, or else when it
  was last changed.

The workflow's own events (steps started and completed, mails sent) are
logged by the workflow as the preset's scenario runs it, and are left alone.
"""

import datetime
import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.logging.middleware import (
    get_event_context,
    reset_event_context,
    set_event_context,
)
from waldur_core.permissions import handlers as permission_handlers
from waldur_core.permissions.models import UserRole
from waldur_mastermind.proposal import activity
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.models import Proposal, Review

logger = logging.getLogger(__name__)

# How long after its creation a submitted proposal with no submission time
# is taken to have been submitted.
SUBMISSION_DELAY = datetime.timedelta(hours=4)
# A team member is added after the proposal exists: its creator first.
TEAM_DELAY = datetime.timedelta(minutes=1)


def _latest_event_id():
    latest = logging_models.Event.objects.order_by("-id").values("id").first()
    return latest["id"] if latest else 0


def _logged_at(after_id, when):
    """Date the events logged since ``after_id`` to ``when``."""
    logging_models.Event.objects.filter(id__gt=after_id).update(created=when)


def _as(user, log):
    """``log()`` as ``user`` did it: the actor the request would name."""
    saved = get_event_context()
    set_event_context(user._get_log_context("user") if user else {})
    try:
        log()
    finally:
        reset_event_context()
        if saved is not None:
            set_event_context(saved)


def _record(user, when, log):
    before = _latest_event_id()
    _as(user, log)
    _logged_at(before, when)


def generate_proposal_history(output=None) -> int:
    """Log the history each proposal's state implies; returns how many
    proposals were given one."""
    now = timezone.now()
    content_type = ContentType.objects.get_for_model(Proposal)
    with_history = set(
        logging_models.Feed.objects.filter(
            content_type=content_type,
            event__event_type=EventType.PROPOSAL_CREATED,
        ).values_list("object_id", flat=True)
    )
    proposals = Proposal.objects.exclude(id__in=with_history).select_related(
        "created_by", "round__call"
    )
    count = 0
    with transaction.atomic():
        for proposal in proposals:
            applicant = proposal.created_by
            created_at = proposal.created or now
            _record(applicant, created_at, lambda p=proposal: activity.log_created(p))
            roles = (
                UserRole.objects.filter(
                    content_type=content_type, object_id=proposal.id, is_active=True
                )
                .select_related("user", "role")
                .order_by("created", "id")
            )
            for user_role in roles:
                _record(
                    applicant,
                    max(user_role.created or created_at, created_at + TEAM_DELAY),
                    lambda r=user_role: permission_handlers.log_role_granted(
                        sender=UserRole,
                        instance=r,
                        current_user=applicant,
                        reason="Added to the proposal team",
                    ),
                )
            if proposal.state != ProposalStates.DRAFT:
                submitted_at = proposal.submitted_at or min(
                    created_at + SUBMISSION_DELAY, now
                )
                if proposal.submitted_at is None:
                    # The history says it was submitted then; the proposal
                    # says so too, or it reads as never submitted.
                    type(proposal).objects.filter(pk=proposal.pk).update(
                        submitted_at=submitted_at
                    )
                _record(
                    applicant,
                    submitted_at,
                    lambda p=proposal: activity.log_submitted(p),
                )
            count += 1
        date_submitted_reviews()
    if output is not None:
        output.write(f"Generated activity histories for {count} proposals\n")
    return count


def date_submitted_reviews() -> int:
    """Give each submitted review without a submission time one: when its
    submission was logged, or else when it was last changed. Returns how
    many reviews were dated."""
    logged = (
        logging_models.Event.objects.filter(
            event_type=EventType.REVIEW_SUBMITTED,
            feed__content_type=ContentType.objects.get_for_model(Review),
            feed__object_id=OuterRef("pk"),
        )
        .order_by("created")
        .values("created")[:1]
    )
    return Review.objects.filter(
        state=Review.States.SUBMITTED, submitted_at__isnull=True
    ).update(submitted_at=Coalesce(Subquery(logged), F("modified")))
