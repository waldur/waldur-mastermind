"""Media access rules for files owned by the proposal app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.marketplace.models import Offering
from waldur_mastermind.proposal.models import (
    CallDocument,
    CallManagingOrganisation,
    Proposal,
    ProposalDocumentation,
    RequestedResource,
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


access.register(
    access.upload_prefix(ProposalDocumentation, "file"),
    user_can_access_proposal_documentation,
)
access.register(
    access.upload_prefix(RequestedResource, "attachment"),
    user_can_access_requested_resource_attachment,
)
