from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.utils import timezone

from waldur_core.core.models import User
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.managers import MixinManager

from . import models
from .enums import CallStates, ProposalStates, RequestedOfferingStates


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


class RequestedOfferingQuerySet(django_models.QuerySet):
    def open_for_proposals(self):
        """Rows through which a proposal can be submitted right now.

        An accepted offering on an active call with a round that is **open**,
        and, when the call defines resource templates, one that covers the
        offering. Keep this the single definition — the ``open_for_proposals``
        field and filter and ``open_for_offering_uuid`` all build on it and must
        agree.

        Matches the write path exactly: ``ProposalSerializer.validate`` accepts
        a proposal only while its round is open, and ``ProposalViewSet.submit``
        applies the same rule. So nothing advertised here can be refused on
        creation, and nothing refused here would have been accepted.
        """
        # Subquery rather than a call__round join, which repeats the row per round.
        now = timezone.now()
        # Mirrors Round.status == OPEN: started, and not yet past its cutoff.
        call_has_open_round = models.Round.objects.filter(
            call=django_models.OuterRef("call"),
            start_time__lte=now,
            cutoff_time__gte=now,
        )
        call_defines_templates = models.CallResourceTemplate.objects.filter(
            call=django_models.OuterRef("call")
        )
        # A template's call FK may differ from its requested offering's, so scope
        # coverage to the row's own call.
        covered_by_template = models.CallResourceTemplate.objects.filter(
            call=django_models.OuterRef("call"),
            requested_offering=django_models.OuterRef("pk"),
        )
        return (
            self.filter(
                django_models.Exists(call_has_open_round),
                state=RequestedOfferingStates.ACCEPTED,
                call__state=CallStates.ACTIVE,
            )
            # alias() rather than annotate(): these are filter-only, and annotate()
            # would push them into the SELECT list of every caller's values().
            .alias(
                _call_defines_templates=django_models.Exists(call_defines_templates),
                _covered_by_template=django_models.Exists(covered_by_template),
            )
            .filter(
                django_models.Q(_call_defines_templates=False)
                | django_models.Q(_covered_by_template=True)
            )
        )

    def offering_ids_open_for_proposals(self):
        return self.open_for_proposals().values_list("offering_id", flat=True)

    def offering_ids_in_active_calls(self):
        """Deprecated predicate behind the ``accessible_via_calls`` filter.

        Broader than ``open_for_proposals()``: it ignores rounds and resource
        template coverage, so it surfaces offerings no proposal can actually be
        submitted for. Kept so the published filter keeps its shipped meaning;
        drop it once no consumer remains.
        """
        return self.filter(
            state=RequestedOfferingStates.ACCEPTED,
            call__state=CallStates.ACTIVE,
        ).values_list("offering_id", flat=True)

    def call_ids_open_for_offering(self, offering_uuid):
        return (
            self.open_for_proposals()
            .filter(offering__uuid=offering_uuid)
            .values_list("call_id", flat=True)
        )


def annotate_offerings_open_for_proposals(queryset):
    """Resolve ``open_for_proposals`` for a marketplace Offering queryset.

    Every view serving offerings through ``PublicOfferingDetailsSerializer``
    should apply this; without it the serializer queries once per offering.
    """
    return queryset.annotate(
        open_for_proposals=django_models.Exists(
            models.RequestedOffering.objects.open_for_proposals().filter(
                offering=django_models.OuterRef("pk")
            )
        )
    )


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
