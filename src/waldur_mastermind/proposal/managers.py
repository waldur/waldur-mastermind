from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models

from waldur_core.core.models import User
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.managers import MixinManager

from . import models
from .enums import ProposalStates, RequestedOfferingStates


class CallQuerySet(django_models.QuerySet):
    def filter_for_user(self, user: User):
        if user.is_anonymous:
            return self.none()

        if user.is_staff or user.is_support:
            return self

        return self.filter(customer__in=get_connected_customers(user))


class CallManager(MixinManager):
    def get_queryset(self):
        return CallQuerySet(self.model, using=self._db)


def get_connected_call_organizers(user):
    ctype = ContentType.objects.get_for_model(models.CallManagingOrganisation)
    return get_scope_ids(user, ctype)


def get_connected_calls(user, role=None):
    ctype = ContentType.objects.get_for_model(models.Call)
    return get_scope_ids(user, ctype, role)


def get_offering_manager_proposals(user):
    """Proposal ids an offering manager (technical reviewer) may read.

    Offering managers hold OFFERING.MANAGER on the Offering, not on the Call, so
    they are invisible to ``get_connected_calls``. They are the responsible role
    for the ``technical_assessment`` step, so they need read access — but only to
    the **non-draft** proposals that actually **requested one of their accepted
    offerings**, not to every (possibly unsubmitted, cross-provider) proposal on
    the call.
    """
    offering_ctype = ContentType.objects.get_for_model(marketplace_models.Offering)
    offering_ids = get_scope_ids(user, offering_ctype, RoleEnum.OFFERING_MANAGER)
    return (
        models.RequestedResource.objects.filter(
            requested_offering__offering_id__in=offering_ids,
            requested_offering__state=RequestedOfferingStates.ACCEPTED,
        )
        .exclude(proposal__state=ProposalStates.DRAFT)
        .values_list("proposal_id", flat=True)
    )
