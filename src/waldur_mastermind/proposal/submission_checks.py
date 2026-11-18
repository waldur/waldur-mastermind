"""Checks another app adds to what a proposal must pass before submission.

The proposal app's own rules live in ``ProposalViewSet.submit_validators``,
the re-checks under the row lock in ``submit`` and ``Proposal.can_submit``.
An app with a rule of its own registers it here, from its
``AppConfig.ready()``, rather than editing those lists::

    from waldur_mastermind.proposal import submission_checks

    def forms_answered(proposal):
        if not all_forms_answered(proposal):
            return "Fill in every form before submitting."
        return None

    submission_checks.register_submission_check("forms", forms_answered)

A check takes the proposal and returns a reason to show the applicant, or
``None`` when the proposal passes. It is consulted wherever the built-in rules
are: by ``Proposal.can_submit`` (the ``can_submit`` the form reads), by the
submit validators, again under the proposal's row lock in ``submit``, and --
when it names the ``parts`` it depends on -- by ``can_submit_changes`` and
``submit_changes``. A refused request is a 400 with ``{key: [reason]}``.
Changes to a post-award run (an extension request, a final report) leave the
accepted proposal as it was and do not consult them.

The reason is visible to anyone who can retrieve the proposal, as
``can_submit.error``, and is the body of the 400. Say what the applicant has
to do; never embed internal data in it (other proposals or users,
configuration, an exception's text).

A check runs for every proposal whose ``can_submit`` is serialised, list views
included, so it must be cheap. It should not raise: one that does is logged
and counts as a failure with a generic reason, so the proposal cannot be
submitted but can still be read.

The dry run of a call's workflow submits a synthetic proposal and does not
consult these checks.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_mastermind.proposal.enums import ChangeRequestParts

logger = logging.getLogger(__name__)

Check = Callable[[Any], str | None]

# The reason of a check that raised: nothing of the error reaches the reader.
CHECK_ERROR_MESSAGE = _(
    "This proposal cannot be submitted right now. Try again later or contact support."
)

# The parts a check may name: those a request for changes to the proposal
# can unlock (change_requests.unlockable_parts). A post-award run's parts are
# not among them: changes to a run do not consult these checks.
CHECKABLE_PARTS = frozenset(
    ChangeRequestParts.OTHER_PARTS
    + tuple(
        ChangeRequestParts.field(name)
        for name in ChangeRequestParts.FIELDS + (ChangeRequestParts.DURATION_FIELD,)
    )
)


@dataclass(frozen=True)
class SubmissionCheck:
    key: str
    check: Check
    # The ChangeRequestParts the check depends on, or None when it applies to
    # the first submission only.
    parts: frozenset[str] | None = None

    def applies_to_changes(self, unlocked_parts) -> bool:
        return self.parts is not None and bool(self.parts & set(unlocked_parts))


# Keyed by the check's key; a dict keeps registration order.
_checks: dict[str, SubmissionCheck] = {}


def register_submission_check(
    key: str, check: Check, parts: Iterable[str] | None = None
) -> None:
    """Register ``check(proposal) -> str | None`` under ``key``.

    ``parts`` names the ``ChangeRequestParts`` the check depends on: with
    them, it is also checked when changes are submitted with one of those
    parts unlocked; without, only on the first submission. A part outside
    ``CHECKABLE_PARTS`` -- a typo, or a post-award run's part -- is a
    ``ValueError``: the check would never apply to changes.

    Registering a key again replaces its check and keeps its place, so a
    module reloaded in tests registers it once.
    """
    if not key or not isinstance(key, str):
        raise ValueError("A submission check needs a non-empty string key.")
    if not callable(check):
        raise TypeError(f"Submission check {key!r} is not callable.")
    if parts is not None:
        if isinstance(parts, str):
            # A bare string would be read as a set of its characters.
            raise TypeError(
                f"Submission check {key!r}: parts must be a collection of "
                "ChangeRequestParts, not a string."
            )
        parts = frozenset(parts)
        if not parts:
            raise ValueError(
                f"Submission check {key!r}: pass parts=None, not an empty "
                "collection, for a check on the first submission only."
            )
        unknown = parts - CHECKABLE_PARTS
        if unknown:
            raise ValueError(
                f"Submission check {key!r}: unknown parts {sorted(unknown)}; "
                f"expected some of {sorted(CHECKABLE_PARTS)}."
            )
    _checks[key] = SubmissionCheck(key=key, check=check, parts=parts)


def unregister_submission_check(key: str) -> None:
    """Remove the check registered under ``key``, if any."""
    _checks.pop(key, None)


def registered_checks() -> list[SubmissionCheck]:
    """The registered checks, in registration order."""
    return list(_checks.values())


def first_failure(proposal, parts=None) -> tuple[str, str] | None:
    """The key and reason of the first registered check ``proposal`` fails.

    With ``parts`` -- the parts unlocked by open requests for changes --
    only the checks depending on one of them are run; without, all of them
    (the first submission).

    A check that raises is logged and counts as failed, with a generic
    reason: submission stays closed, and the proposal -- whose
    ``can_submit`` runs the checks -- can still be read.
    """
    for registered in list(_checks.values()):
        if parts is not None and not registered.applies_to_changes(parts):
            continue
        try:
            reason = registered.check(proposal)
        except Exception:
            logger.exception(
                "Submission check %r failed on proposal %s.",
                registered.key,
                getattr(proposal, "uuid", None),
            )
            return registered.key, CHECK_ERROR_MESSAGE
        if reason:
            return registered.key, reason
    return None


def validate(proposal, parts=None) -> None:
    """Raise a ``ValidationError`` ``{key: [reason]}`` for the first
    registered check ``proposal`` fails. ``parts`` as in ``first_failure``."""
    failure = first_failure(proposal, parts)
    if failure:
        key, reason = failure
        raise exceptions.ValidationError({key: [reason]})
