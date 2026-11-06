"""The project side of an award: which proposal, of which call, a project was
allocated for.

A proposal points at its project (``Proposal.project``); a project does not
point back, and the project API is the structure app's, which knows nothing of
calls. This adds ``awarded_proposal`` to the project serializer from here, so
a project page can say where the project came from and link to it.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalStates


class AwardedProposalSerializer(serializers.Serializer):
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    round_uuid = serializers.UUIDField()
    round_name = serializers.CharField()
    can_read = serializers.BooleanField(
        help_text=(
            "Whether the viewer may open the proposal: its team and the call's "
            "overseers do; a collaborator added to the project later sees only "
            "where it came from."
        )
    )


def awarded_proposals(project_ids, user=None) -> dict:
    """The proposal each project was allocated for, keyed by project id, and
    whether ``user`` may open it.

    A project is allocated for one proposal; should several point at it, the
    accepted one wins, then the latest.
    """
    rows = (
        models.Proposal.objects.filter(project_id__in=project_ids)
        .select_related("round__call")
        .order_by("project_id", "created")
    )
    result = {}
    for proposal in rows:
        current = result.get(proposal.project_id)
        if (
            current is not None
            and current["_accepted"]
            and proposal.state != ProposalStates.ACCEPTED
        ):
            continue
        result[proposal.project_id] = {
            "_accepted": proposal.state == ProposalStates.ACCEPTED,
            "proposal_uuid": proposal.uuid.hex,
            "proposal_name": proposal.name,
            "call_uuid": proposal.round.call.uuid.hex,
            "call_name": proposal.round.call.name,
            "round_uuid": proposal.round.uuid.hex,
            "round_name": proposal.round.name,
        }
    # The same rule the proposal endpoint reads with, so the link is offered
    # exactly where it opens.
    readable = set()
    if user is not None and result:
        readable = {
            uuid.hex
            for uuid in filter_queryset_for_user(
                models.Proposal.objects.filter(
                    uuid__in=[info["proposal_uuid"] for info in result.values()]
                ),
                user,
            ).values_list("uuid", flat=True)
        }
    return {
        project_id: {
            **{key: value for key, value in info.items() if key != "_accepted"},
            "can_read": info["proposal_uuid"] in readable,
        }
        for project_id, info in result.items()
    }


@extend_schema_field(AwardedProposalSerializer(allow_null=True))
def get_awarded_proposal(serializer, project):
    bulk_data = serializer.context.get("bulk_data", {})
    if "awarded_proposals" in bulk_data:
        return bulk_data["awarded_proposals"].get(project.id)
    return awarded_proposals([project.id], _viewer(serializer)).get(project.id)


def _viewer(serializer):
    request = serializer.context.get("request")
    return getattr(request, "user", None)


def add_awarded_proposal(sender, fields, **kwargs):
    fields["awarded_proposal"] = serializers.SerializerMethodField()
    setattr(sender, "get_awarded_proposal", get_awarded_proposal)
