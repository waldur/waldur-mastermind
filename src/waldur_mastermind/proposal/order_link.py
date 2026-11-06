"""The award behind an order: which proposal, of which call, a marketplace
order provisions, and what it was awarded against what was asked.

Allocation places the orders of an award like any other order, so the
provider reviews them in its ordinary order list. That list is the
marketplace's and knows nothing of calls; this adds ``award`` to the order
serializer from here (as ``project_link`` does for projects), so the
provider sees the call and proposal an order comes from before approving it.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.proposal import models


class OrderAwardSerializer(serializers.Serializer):
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    round_name = serializers.CharField()
    awarded_limits = serializers.DictField(
        help_text="What the award grants on the order's offering."
    )
    requested_limits = serializers.DictField(
        allow_null=True,
        help_text="What the applicant asked for; null for an item the call manager added.",
    )
    can_read = serializers.BooleanField(
        help_text="Whether the viewer may open the proposal."
    )


def awards_by_resource(resource_ids) -> dict:
    items = (
        models.AwardedResource.objects.filter(resource_id__in=resource_ids)
        .select_related("proposal__round__call", "requested_resource")
        .order_by("created")
    )
    return {
        item.resource_id: {
            "proposal_uuid": item.proposal.uuid.hex,
            "proposal_name": item.proposal.name,
            "call_uuid": item.proposal.round.call.uuid.hex,
            "call_name": item.proposal.round.call.name,
            "round_name": item.proposal.round.name,
            "awarded_limits": item.limits or {},
            "requested_limits": (
                item.requested_resource.limits if item.requested_resource else None
            ),
        }
        for item in items
    }


def _can_read(serializer, proposal_uuid) -> bool:
    """Whether the viewer may open the proposal, asked once per proposal of
    the page being serialized."""
    request = serializer.context.get("request")
    user = getattr(request, "user", None)
    if user is None:
        return False
    cache = serializer.root.__dict__.setdefault("_readable_proposals", {})
    if proposal_uuid not in cache:
        cache[proposal_uuid] = filter_queryset_for_user(
            models.Proposal.objects.filter(uuid=proposal_uuid), user
        ).exists()
    return cache[proposal_uuid]


_MISSING = object()


def attach_awards(orders):
    """Page loader: the awards of a page of orders, in one query."""
    awards = awards_by_resource({order.resource_id for order in orders})
    for order in orders:
        order._award = awards.get(order.resource_id)


@extend_schema_field(OrderAwardSerializer(allow_null=True))
def get_award(serializer, order):
    award = getattr(order, "_award", _MISSING)
    if award is _MISSING:
        award = awards_by_resource([order.resource_id]).get(order.resource_id)
    if award is None:
        return None
    return {**award, "can_read": _can_read(serializer, award["proposal_uuid"])}


def add_award(sender, fields, **kwargs):
    fields["award"] = serializers.SerializerMethodField()
    setattr(sender, "get_award", get_award)
