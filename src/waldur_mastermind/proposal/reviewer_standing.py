"""Who may be given new review work: the operator's standing, an active
account, and the call's own reviewer eligibility.

Selection paths start from ``ReviewerProfile.objects.eligible()`` or
``CallReviewerPool.objects.eligible()``; the checks here are the last line at
the points where review work is taken on -- accepting a pool invitation or an
assignment, creating a review, granting a reviewing role -- so a path that
picked a reviewer before their standing changed still stops there.
"""

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.configuration import config
from waldur_core.permissions.utils import validate_user_restrictions
from waldur_mastermind.proposal import enums, models

NOT_IN_GOOD_STANDING = _(
    "This reviewer is not in good standing on the platform and cannot take on "
    "new review work."
)
ACCOUNT_INACTIVE = _("This reviewer's account is not active.")


def standing_error(user) -> str | None:
    """Why ``user`` may not take on new review work on the platform, or None.

    A user without a reviewer profile has no standing to lose: the CALL.REVIEWER
    role and the evaluator roles are granted without one.
    """
    if not user.is_active:
        return str(ACCOUNT_INACTIVE)
    profile = models.ReviewerProfile.objects.filter(user=user).first()
    if profile is not None and profile.standing != enums.ReviewerStandings.ACTIVE:
        return str(NOT_IN_GOOD_STANDING)
    return None


def eligibility_error(call, user) -> str | None:
    """Why ``user`` does not meet ``call``'s reviewer eligibility, or None.

    Only the call's reviewer rules (CallReviewerEligibility) are read, never
    the restrictions it places on applicants.
    """
    eligibility = getattr_or_none(call, "reviewer_eligibility")
    if eligibility is None:
        return None
    try:
        validate_user_restrictions(eligibility, user)
    except ValidationError:
        return str(
            _("This reviewer does not meet the call's reviewer eligibility rules.")
        )
    return None


def ensure_reviewer_eligible(user, call=None):
    """Refuse new review work for ``user`` (on ``call``, when given)."""
    error = standing_error(user) or (call and eligibility_error(call, user))
    if error:
        raise ValidationError(error)


def admission_check(call):
    """A predicate telling whether ``call``'s reviewer eligibility admits a
    user -- for filtering candidates rather than refusing a request. The
    call's rules are read once, so filtering many candidates costs no query
    per candidate."""
    eligibility = getattr_or_none(call, "reviewer_eligibility")
    if eligibility is None:
        return lambda user: True

    def admits(user) -> bool:
        try:
            validate_user_restrictions(eligibility, user)
        except ValidationError:
            return False
        return True

    return admits


def ensure_coi_disclosed(user, call):
    """With COI_DISCLOSURE_REQUIRED on, a reviewer reviews a call's proposals
    only after a certified, current disclosure for that call."""
    if not config.COI_DISCLOSURE_REQUIRED:
        return
    disclosed = models.COIDisclosureForm.objects.filter(
        reviewer__user=user,
        call=call,
        certified=True,
        is_current=True,
        valid_until__gte=timezone.now().date(),
    ).exists()
    if not disclosed:
        raise ValidationError(
            _(
                "A certified conflict of interest disclosure for this call is "
                "required before reviewing its proposals."
            )
        )


def getattr_or_none(obj, name):
    try:
        return getattr(obj, name)
    except models.CallReviewerEligibility.DoesNotExist:
        return None


def ensure_reviewer_profiles_enabled():
    if not config.REVIEWER_PROFILES_ENABLED:
        raise ValidationError(_("Reviewer profiles are turned off on this platform."))


def ensure_automated_matching_enabled():
    if not config.AUTOMATED_MATCHING_ENABLED:
        raise ValidationError(
            _("Automated reviewer matching is turned off on this platform.")
        )


def ensure_coi_detection_enabled():
    if not config.COI_DETECTION_ENABLED:
        raise ValidationError(
            _(
                "Automated conflict of interest detection is turned off on this platform."
            )
        )
