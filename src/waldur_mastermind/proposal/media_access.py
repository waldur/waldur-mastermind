"""Media access rules for files owned by the proposal app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.marketplace.models import Offering
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.models import (
    Call,
    CallDocument,
    CallManagingOrganisation,
    Proposal,
    ProposalDocumentation,
    RequestedResource,
    Round,
)

# CallManagingOrganisationViewSet is a PublicViewsetMixin listing.
access.register_public(access.image_prefix(CallManagingOrganisation))

# Call documents are embedded in PublicCallSerializer, served anonymously by
# PublicCallViewSet for active and archived calls.
access.register_public(access.upload_prefix(CallDocument, "file"))


def user_can_access_proposal_documentation(file, user) -> bool:
    """Mirror ProposalViewSet.get_queryset, which owns the parent proposal."""
    if not user.is_authenticated:
        return False
    proposals = filter_queryset_for_user(Proposal.objects.all(), user)
    return ProposalDocumentation.objects.filter(
        file=file.name, proposal__in=proposals
    ).exists()


def user_can_access_requested_resource_attachment(file, user) -> bool:
    """Union of the consumer and provider views of a requested resource.

    ``UserRequestedResourceViewSet`` scopes through the parent proposal;
    ``ProviderRequestedResourceViewSet`` scopes through the requested offering.
    A purchase order attached here is legitimately visible to both sides, so
    either route grants access.
    """
    if not user.is_authenticated:
        return False

    queryset = RequestedResource.objects.filter(attachment=file.name)
    if user.is_staff or user.is_support:
        return queryset.exists()

    proposals = filter_queryset_for_user(Proposal.objects.all(), user)
    if queryset.filter(proposal__in=proposals).exists():
        return True

    offering_ids = Offering.objects.all().filter_for_user(user).values_list("id")
    return queryset.filter(requested_offering__offering_id__in=offering_ids).exists()


def user_can_access_round_adoption_document(file, user) -> bool:
    """Mirror ProtectedRoundSerializer: the round is served by
    ProtectedCallViewSet.get_queryset, and its adoption record only to those
    who may read it yet."""
    if not user.is_authenticated:
        return False
    calls = filter_queryset_for_user(Call.objects.all(), user)
    rounds = Round.objects.filter(
        adoption_document=file.name, call__in=calls
    ).select_related("call")
    return any(
        proposal_permissions.user_can_view_round_adoption(user, call_round)
        for call_round in rounds
    )


access.register(
    access.upload_prefix(Round, "adoption_document"),
    user_can_access_round_adoption_document,
)
access.register(
    access.upload_prefix(ProposalDocumentation, "file"),
    user_can_access_proposal_documentation,
)
access.register(
    access.upload_prefix(RequestedResource, "attachment"),
    user_can_access_requested_resource_attachment,
)
