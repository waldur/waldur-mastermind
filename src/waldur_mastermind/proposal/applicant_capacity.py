"""How many proposals an applicant may hold at once with a call-managing
organization.

An applicant is the proposal's creator (``Proposal.created_by``), not every
member or co-manager of its team. A proposal holds a place while it is under
evaluation, and once accepted while its project runs: until the project is
completed, removed or past its end date. Drafts, rejected and canceled
proposals hold none, and an accepted proposal and its project are one place,
not two.
"""

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalStates


def held_places(applicant, manager):
    """The applicant's proposals that hold a place with ``manager``."""
    today = timezone.localdate()
    running_project = Q(project__isnull=True) | (
        Q(project__is_removed=False)
        & (Q(project__end_date__isnull=True) | Q(project__end_date__gte=today))
    )
    return models.Proposal.objects.filter(
        created_by=applicant, round__call__manager=manager
    ).filter(
        Q(state__in=ProposalStates.UNDER_EVALUATION)
        | (
            Q(state=ProposalStates.ACCEPTED, completed_at__isnull=True)
            & running_project
        )
    )


def validate_applicant_capacity(call, applicant):
    """Refuse ``applicant`` another proposal to ``call`` once they hold as
    many places as the call's managing organization allows."""
    limit = call.manager.max_active_proposals_per_applicant
    if limit is None or applicant is None:
        return
    if held_places(applicant, call.manager).count() >= limit:
        raise exceptions.ValidationError(
            _(
                "You have reached the maximum of %(limit)s active projects or "
                "pending proposals. You cannot submit another proposal until "
                "one of them is completed or closed."
            )
            % {"limit": limit}
        )
