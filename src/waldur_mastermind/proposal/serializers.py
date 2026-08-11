import logging
import re
from datetime import datetime

from constance import config
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.drainage import set_override
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.reverse import reverse

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.core import serializers as core_serializers
from waldur_core.core.validators import get_project_name_regex_error
from waldur_core.permissions import enums as permissions_enums
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import Role
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.serializers import (
    BasePublicPlanSerializer,
    OfferingComponentSerializer,
    OfferingOptionsField,
    UserAttributeConfigBaseSerializer,
)
from waldur_mastermind.proposal.enums import (
    MANDATORY_STEPS,
    WORKFLOW_STEPS_MAP,
    AllocationTimes,
    BulkRoundCadence,
    CallStates,
    COISeverityLevels,
    COITypes,
    ProposalStates,
    RequestedOfferingStates,
    ReviewerPoolInvitationStatuses,
    RoundStatuses,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

from . import models, workflow_service
from .managers import get_connected_calls

logger = logging.getLogger(__name__)


# Maps applicant attribute name (from CallApplicantVisibilityConfig.expose_*)
# to ProposalSerializer field names that should be filtered when a reviewer
# views a proposal and the attribute is not exposed.
APPLICANT_FIELD_MAP: dict[str, list[str]] = {
    "full_name": [
        "created_by_name",
        "applicant_full_name",
        "applicant_first_name",
        "applicant_last_name",
    ],
    "username": [
        "created_by",
        "created_by_uuid",
        "applicant_username",
    ],
    "email": ["applicant_email"],
    "registration_method": ["applicant_registration_method"],
    "phone_number": ["applicant_phone_number"],
    "organization": ["applicant_organization"],
    "organization_country": ["applicant_organization_country"],
    "organization_type": ["applicant_organization_type"],
    "organization_registry_code": ["applicant_organization_registry_code"],
    "organization_vat_code": ["applicant_organization_vat_code"],
    "organization_address": ["applicant_organization_address"],
    "job_title": ["applicant_job_title"],
    "affiliations": ["applicant_affiliations"],
    "gender": ["applicant_gender"],
    "personal_title": ["applicant_personal_title"],
    "place_of_birth": ["applicant_place_of_birth"],
    "address": ["applicant_address"],
    "country_of_residence": ["applicant_country_of_residence"],
    "nationality": ["applicant_nationality"],
    "nationalities": ["applicant_nationalities"],
    "eduperson_assurance": ["applicant_eduperson_assurance"],
    "identity_source": ["applicant_identity_source"],
    "civil_number": ["applicant_civil_number"],
    "birth_date": ["applicant_birth_date"],
    "active_isds": ["applicant_active_isds"],
}


def _is_reviewer_only_view(user, proposal) -> bool:
    """True if the user views this proposal solely as a reviewer.

    Returns False for the applicant, call managers, staff, support, and
    anonymous users — all of whom should see unfiltered data.
    """
    if not user or user.is_anonymous:
        return False
    if user.is_staff or user.is_support:
        return False
    if proposal.created_by_id == user.id:
        return False
    call_id = proposal.round.call_id
    if call_id in get_connected_calls(user, CallRole.MANAGER):
        return False
    return call_id in get_connected_calls(user, CallRole.REVIEWER)


def filter_applicant_fields_for_reviewer(data: dict, proposal, user) -> dict:
    """Mutate the serialized representation to drop applicant fields that
    are not exposed by the call's visibility config when the user is a
    reviewer-only viewer."""
    if not _is_reviewer_only_view(user, proposal):
        return data
    exposed = models.CallApplicantVisibilityConfig.get_exposed_fields_for_call(
        proposal.round.call
    )
    kept_serializer_fields: set[str] = set()
    for attr in exposed:
        kept_serializer_fields.update(APPLICANT_FIELD_MAP.get(attr, []))
    all_filterable: set[str] = set()
    for serializer_fields in APPLICANT_FIELD_MAP.values():
        all_filterable.update(serializer_fields)
    for field_name in all_filterable - kept_serializer_fields:
        data.pop(field_name, None)
    return data


class EligibilityCheckSerializer(serializers.Serializer):
    """Serializer for eligibility check response."""

    is_eligible = serializers.BooleanField()
    restrictions = serializers.ListField(child=serializers.CharField())


class NestedCallActionHyperlinkedRelatedField(serializers.HyperlinkedRelatedField):
    """
    HyperlinkedRelatedField for nested call actions that require two lookup fields:
    - uuid: the call's UUID (parent)
    - obj_uuid: the nested object's UUID (child)
    """

    def get_url(self, obj, view_name, request, format):
        if hasattr(obj, "pk") and obj.pk in (None, ""):
            return None

        kwargs = {
            "uuid": obj.call.uuid.hex,
            "obj_uuid": obj.uuid.hex,
        }
        return self.reverse(view_name, kwargs=kwargs, request=request, format=format)

    def get_object(self, view_name, view_args, view_kwargs):
        lookup_value = view_kwargs.get("obj_uuid")
        lookup_kwargs = {self.lookup_field: lookup_value}
        return self.get_queryset().get(**lookup_kwargs)


class CallManagingOrganisationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.CallManagingOrganisation
        fields = (
            "url",
            "uuid",
            "created",
            "description",
            "customer",
            "customer_name",
            "customer_uuid",
            "customer_image",
            "customer_abbreviation",
            "customer_native_name",
            "customer_country",
            "image",
        )
        related_paths = {"customer": ("uuid", "name", "native_name", "abbreviation")}
        protected_fields = ("customer",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "customer": {"lookup_field": "uuid"},
        }

    customer_image = serializers.ImageField(source="customer.image", read_only=True)
    customer_country = serializers.CharField(source="customer.country", read_only=True)

    def validate(self, attrs):
        if not self.instance:
            marketplace_permissions.can_register_service_provider(
                self.context["request"], attrs["customer"]
            )
        return attrs


class NestedRequestedOfferingSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    # The plugin type drives the frontend's per-type component filter, which a
    # cost estimate has to apply or it prices components the offering hides.
    offering_type = serializers.ReadOnlyField(source="offering.type")
    category_uuid = serializers.UUIDField(
        read_only=True, source="offering.category.uuid"
    )
    category_name = serializers.ReadOnlyField(source="offering.category.title")
    provider_name = serializers.ReadOnlyField(source="offering.customer.name")
    call_managing_organisation = serializers.ReadOnlyField(
        source="call.manager.customer.name"
    )
    options = OfferingOptionsField(read_only=True, source="offering.options")
    plan_details = BasePublicPlanSerializer(read_only=True, source="plan")
    components = OfferingComponentSerializer(
        source="offering.components", many=True, read_only=True
    )

    class Meta:
        model = models.RequestedOffering
        fields = [
            "uuid",
            "state",
            "offering",
            "offering_name",
            "offering_uuid",
            "offering_type",
            "provider_name",
            "category_uuid",
            "category_name",
            "call_managing_organisation",
            "attributes",
            "plan",
            "plan_details",
            "options",
            "components",
            "require_purchase_order",
            "created",
        ]
        extra_kwargs = {
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-public-offering-detail",
            },
            "plan": {
                "lookup_field": "uuid",
                "view_name": "marketplace-plan-detail",
            },
        }

    def get_url(self, requested_offering) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-offering-detail",
                kwargs={
                    "uuid": requested_offering.call.uuid.hex,
                    "obj_uuid": requested_offering.uuid.hex,
                },
            )
        )


class NestedRequestedResourceSerializer(serializers.HyperlinkedModelSerializer):
    resource_name = serializers.ReadOnlyField(source="resource.name")
    requested_offering = NestedRequestedOfferingSerializer(read_only=True)
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    url = serializers.SerializerMethodField()
    call_resource_template_name = serializers.ReadOnlyField(
        source="call_resource_template.name"
    )
    call_resource_template = serializers.SerializerMethodField()

    def get_url(self, requested_resource) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-proposal-resource-detail",
                kwargs={
                    "uuid": requested_resource.proposal.uuid.hex,
                    "obj_uuid": requested_resource.uuid.hex,
                },
            )
        )

    def get_call_resource_template(self, requested_resource) -> str:
        if requested_resource.call_resource_template:
            return self.context["request"].build_absolute_uri(
                reverse(
                    "proposal-call-resource_template-detail",
                    kwargs={
                        "uuid": requested_resource.call_resource_template.call.uuid.hex,
                        "obj_uuid": requested_resource.call_resource_template.uuid.hex,
                    },
                )
            )
        return None

    # Whether this row needs a purchase order, resolved from the call's setting
    # so the form does not have to re-derive it from plugin_options.
    purchase_order_required = serializers.ReadOnlyField()
    has_purchase_order = serializers.ReadOnlyField()
    # Written through the dedicated multipart action, as orders do.
    attachment = serializers.FileField(read_only=True)

    class Meta:
        model = models.RequestedResource
        fields = [
            "uuid",
            "url",
            "requested_offering",
            "resource",
            "resource_name",
            "call_resource_template",
            "call_resource_template_name",
            "attributes",
            "limits",
            "purchase_order_reference",
            "attachment",
            "purchase_order_required",
            "has_purchase_order",
            "description",
            "created_by",
            "created_by_name",
        ]
        extra_kwargs = {
            "resource": {
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
            "created_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }


class UserRequestedResourceSerializer(serializers.HyperlinkedModelSerializer):
    """One row of "resources I requested through a proposal".

    Deliberately flat rather than reusing ``NestedRequestedResourceSerializer``:
    that one embeds the whole requested offering (plan details, components,
    options), which is far more than a list needs and costs a query per row.

    Proposal state and resource state are reported separately. They are two
    different lifecycles — the resource does not exist until the proposal is
    approved — so collapsing them into one column would require inventing a
    mapping that neither model owns.
    """

    offering_name = serializers.CharField(
        read_only=True, source="requested_offering.offering.name"
    )
    offering_uuid = serializers.UUIDField(
        read_only=True, source="requested_offering.offering.uuid"
    )
    call_name = serializers.CharField(read_only=True, source="proposal.round.call.name")
    call_uuid = serializers.UUIDField(read_only=True, source="proposal.round.call.uuid")
    proposal_name = serializers.CharField(read_only=True, source="proposal.name")
    proposal_uuid = serializers.UUIDField(read_only=True, source="proposal.uuid")
    proposal_state = serializers.CharField(read_only=True, source="proposal.state")
    # resource is null until the proposal is approved. Without allow_null DRF
    # raises SkipField on the dotted source and drops the key from the payload
    # entirely, so the SDK sees an absent field rather than an explicit null.
    resource_name = serializers.CharField(
        read_only=True, source="resource.name", allow_null=True
    )
    resource_uuid = serializers.UUIDField(
        read_only=True, source="resource.uuid", allow_null=True
    )
    resource_state = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_resource_state(self, requested_resource) -> str | None:
        """Null until the proposal is approved and the resource is provisioned."""
        if not requested_resource.resource:
            return None
        return requested_resource.resource.get_state_display()

    class Meta:
        model = models.RequestedResource
        fields = [
            "uuid",
            "created",
            "description",
            "attributes",
            "limits",
            "offering_name",
            "offering_uuid",
            "call_name",
            "call_uuid",
            "proposal",
            "proposal_name",
            "proposal_uuid",
            "proposal_state",
            "resource_name",
            "resource_uuid",
            "resource_state",
        ]
        extra_kwargs = {
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
        }


class ProposalReviewSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round_uuid = serializers.UUIDField(source="proposal.round.uuid", read_only=True)
    round_cutoff_time = serializers.ReadOnlyField(source="proposal.round.cutoff_time")
    round_start_time = serializers.ReadOnlyField(source="proposal.round.start_time")
    round_name = serializers.ReadOnlyField(source="proposal.round.name")
    round_slug = serializers.ReadOnlyField(source="proposal.round.slug")
    call_uuid = serializers.UUIDField(source="proposal.round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="proposal.round.call.name")
    call_slug = serializers.ReadOnlyField(source="proposal.round.call.slug")
    call_managing_organisation_uuid = serializers.ReadOnlyField(
        source="proposal.round.call.manager.uuid"
    )
    reviewer_full_name = serializers.ReadOnlyField(source="reviewer.full_name")
    reviewer_uuid = serializers.UUIDField(read_only=True, source="reviewer.uuid")
    reviewer_image = serializers.ImageField(source="reviewer.image", read_only=True)
    anonymous_reviewer_name = serializers.SerializerMethodField()

    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(read_only=True, source="proposal.uuid")
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    coi_confirmation_required = serializers.SerializerMethodField()

    class Meta:
        model = models.Review
        fields = (
            "url",
            "uuid",
            "proposal",
            "proposal_name",
            "proposal_uuid",
            "proposal_slug",
            "reviewer",
            "reviewer_full_name",
            "reviewer_uuid",
            "reviewer_image",
            "anonymous_reviewer_name",
            "state",
            "review_end_date",
            "summary_score",
            "summary_public_comment",
            "summary_private_comment",
            "round_uuid",
            "round_name",
            "round_slug",
            "round_cutoff_time",
            "round_start_time",
            "call_name",
            "call_uuid",
            "call_slug",
            "call_managing_organisation_uuid",
            "comment_project_title",
            "comment_project_summary",
            "comment_project_is_confidential",
            "comment_project_has_civilian_purpose",
            "comment_project_description",
            "comment_project_duration",
            "comment_project_supporting_documentation",
            "comment_resource_requests",
            "comment_team",
            "coi_confirmed",
            "coi_confirmed_at",
            "coi_confirmation_required",
            "created",
            "modified",
        )
        read_only_fields = ("coi_confirmed", "coi_confirmed_at")
        protected_fields = ("proposal", "reviewer")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }

    def validate(self, attrs):
        if not self.instance:
            reviewer = attrs["reviewer"]
            proposal = attrs["proposal"]

            if reviewer not in proposal.round.call.reviewers:
                raise serializers.ValidationError(
                    {"reviewer": _("User is not reviewer.")}
                )

        return attrs

    @extend_schema_field(serializers.BooleanField())
    def get_coi_confirmation_required(self, obj) -> bool:
        # True when the call has an enabled workflow step that requires the
        # reviewer to attest absence of conflict of interest. Drives the
        # confirmation checkbox in the review-submission UI.
        return models.CallWorkflowStep.objects.filter(
            call=obj.proposal.round.call_id,
            is_enabled=True,
            requires_coi_confirmation=True,
        ).exists()

    def get_anonymous_reviewer_name(self, obj) -> str | None:
        """
        Generate an anonymous reviewer identifier like 'Reviewer 1', 'Reviewer 2'.
        Returns None if the review is not associated with a proposal.
        """
        if not obj.proposal:
            return None

        # Get all reviews for the proposal in a stable order
        reviews = obj.proposal.review_set.order_by("created", "reviewer__id")

        for index, review in enumerate(reviews, start=1):
            if review.pk == obj.pk:
                return _("Reviewer %(index)s") % {"index": index}

        return _("Reviewer")

    def get_fields(self):
        fields = super().get_fields()

        if not self.instance:
            return fields
        elif isinstance(self.instance, list):
            review = self.instance[0]
        elif isinstance(self.instance, QuerySet):
            review = self.instance.last()
        else:
            review: models.Review = self.instance

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if (
            user.is_staff
            or review.reviewer == user
            or review.proposal.round.call.manager.customer.has_user(user)
            or review.proposal.round.call.has_user(user, CallRole.MANAGER)
        ):
            fields.pop("anonymous_reviewer_name", None)
            return fields

        # For proposal submitters, apply reviewer identity visibility control
        is_proposal_submitter = review.proposal.created_by == user
        call = review.proposal.round.call

        if is_proposal_submitter and not call.reviewer_identity_visible_to_submitters:
            # Hide real reviewer info, show anonymous identifier
            fields.pop("reviewer", None)
            fields.pop("reviewer_full_name", None)
            fields.pop("reviewer_uuid", None)
            fields.pop("reviewer_image", None)
        else:
            # Show real reviewer info, hide anonymous identifier
            fields.pop("anonymous_reviewer_name", None)

        # Always remove private comments for non-authorized users
        fields.pop("summary_private_comment", None)

        return fields

    def create(self, validated_data):
        """
        Prevent creating a duplicate review for the same proposal and reviewer, excluding rejected reviews.
        """
        reviewer = validated_data["reviewer"]
        proposal = validated_data["proposal"]

        existing_review = models.Review.objects.filter(
            proposal=proposal,
            reviewer=reviewer,
            state__in=[
                models.Review.States.SUBMITTED,
                models.Review.States.IN_REVIEW,
            ],
        ).exists()

        if existing_review:
            raise serializers.ValidationError(
                _("Review already exists for this proposal and reviewer.")
            )

        return super().create(validated_data)


set_override(
    ProposalReviewSerializer,
    "optional_fields",
    [
        "anonymous_reviewer_name",
        "reviewer",
        "reviewer_full_name",
        "reviewer_uuid",
        "summary_private_comment",
    ],
)


class ReviewSubmitSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Review
        fields = (
            "summary_score",
            "summary_public_comment",
            "summary_private_comment",
            "coi_confirmed",
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        review = self.instance
        # A conflict-of-interest attestation is required only when the call has
        # an enabled workflow step configured with requires_coi_confirmation.
        # The flag is per-step, but the attestation is per-review, so any such
        # enabled step on the call triggers the requirement.
        requires_coi = models.CallWorkflowStep.objects.filter(
            call=review.proposal.round.call_id,
            is_enabled=True,
            requires_coi_confirmation=True,
        ).exists()
        coi_confirmed = attrs.get("coi_confirmed", review.coi_confirmed)
        if requires_coi and not coi_confirmed:
            raise serializers.ValidationError(
                {
                    "coi_confirmed": _(
                        "You must confirm absence of conflict of interest "
                        "before submitting this review."
                    )
                }
            )
        return attrs

    def update(self, instance, validated_data):
        if "coi_confirmed" in validated_data:
            if validated_data["coi_confirmed"]:
                if not instance.coi_confirmed_at:
                    validated_data["coi_confirmed_at"] = timezone.now()
            else:
                # Keep the timestamp consistent with the flag: an unconfirmed
                # review must not carry a stale confirmation time.
                validated_data["coi_confirmed_at"] = None
        return super().update(instance, validated_data)


class ProtectedProposalListSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    approved_by_name = serializers.ReadOnlyField(source="approved_by.full_name")
    reviews = serializers.SerializerMethodField()

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
            "slug",
            "name",
            "state",
            "reviews",
            "approved_by_name",
            "created_by_name",
            "created",
        ]
        extra_kwargs = {
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "approved_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_reviews(self, obj) -> list:
        """
        Return serialized reviews based on user permissions and visibility settings.
        - Staff, call managers, and reviewers see all reviews.
        - Submitters see submitted reviews if visibility is enabled.
        """
        request = self.context.get("request")
        user = getattr(request, "user", None)

        if not user or not request:
            return []

        reviews_qs = obj.review_set.all()

        if (
            user.is_staff
            or obj.round.call.manager.customer.has_user(user)
            or reviews_qs.filter(reviewer=user).exists()
        ):
            return ProposalReviewSerializer(
                reviews_qs, many=True, context=self.context
            ).data

        # Submitter logic
        if (
            obj.created_by == user
            and obj.round.call.reviews_visible_to_submitters
            and (
                obj.state == models.Proposal.States.ACCEPTED
                or obj.state == models.Proposal.States.REJECTED
            )
        ):
            submitted_reviews = reviews_qs.filter(state=models.Review.States.SUBMITTED)
            return ProposalReviewSerializer(
                submitted_reviews, many=True, context=self.context
            ).data

        return []


class NestedRoundSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Round
        fields = [
            "uuid",
            "slug",
            "name",
            "start_time",
            "cutoff_time",
            "status",
            "allocation_date",
            "review_duration_in_days",
        ]
        extra_kwargs = {
            "slug": {"required": False},
        }


class CallDocumentSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = serializers.IntegerField(source="file.size", read_only=True)

    class Meta:
        model = models.CallDocument
        fields = ["uuid", "file", "file_name", "file_size", "description", "created"]


class CallNotArchivedCreateMixin:
    """Provide the ``validate_call_not_archived`` hook used by
    ``ActionMethodMixin.action_list_method``'s ``additional_validators``.

    The hook is looked up by name on the serializer and called with the parent
    Call. It keeps archived calls read-only across their nested-create surface
    (offerings / resource templates / workflow steps).
    """

    def validate_call_not_archived(self, call):
        if call.state == CallStates.ARCHIVED:
            raise serializers.ValidationError(_("Cannot modify an archived call."))


class CallResourceTemplateSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    requested_offering_name = serializers.ReadOnlyField(
        source="requested_offering.offering.name"
    )
    requested_offering_plan = BasePublicPlanSerializer(
        read_only=True, source="requested_offering.plan"
    )
    requested_offering_uuid = serializers.UUIDField(
        source="requested_offering.uuid", read_only=True
    )
    # The plan alone cannot be priced: bucketing an amount into recurring or
    # one-off needs each component's billing type and limit period, and the
    # plugin type drives the frontend's component filter. Same two fields
    # NestedRequestedOfferingSerializer carries for the non-template path.
    requested_offering_type = serializers.ReadOnlyField(
        source="requested_offering.offering.type"
    )
    requested_offering_components = OfferingComponentSerializer(
        source="requested_offering.offering.components", many=True, read_only=True
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    url = serializers.SerializerMethodField()
    requested_offering = NestedCallActionHyperlinkedRelatedField(
        queryset=models.RequestedOffering.objects.all(),
        view_name="proposal-call-offering-detail",
        lookup_field="uuid",
    )
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)

    class Meta:
        model = models.CallResourceTemplate
        fields = [
            "uuid",
            "url",
            "name",
            "description",
            "attributes",
            "limits",
            "is_required",
            "requested_offering",
            "requested_offering_name",
            "requested_offering_uuid",
            "requested_offering_plan",
            "requested_offering_type",
            "requested_offering_components",
            "created_by",
            "created_by_name",
            "created",
        ]
        read_only_fields = ("created_by",)
        extra_kwargs = {
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_fields(self):
        """Make requested_offering not required for PATCH operations."""
        fields = super().get_fields()
        if hasattr(self, "instance") and self.instance:
            if "requested_offering" in fields:
                fields["requested_offering"].required = False

        return fields

    def get_url(self, resource_template) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-resource_template-detail",
                kwargs={
                    "uuid": resource_template.call.uuid.hex,
                    "obj_uuid": resource_template.uuid.hex,
                },
            )
        )

    def validate_requested_offering(self, requested_offering):
        if hasattr(self, "initial_data") and "call" in self.context:
            call = self.context["call"]
            if requested_offering.call != call:
                raise serializers.ValidationError(
                    "Requested offering must belong to the same call"
                )
            if requested_offering.state != RequestedOfferingStates.ACCEPTED:
                raise serializers.ValidationError(
                    "Requested offering must be in accepted state"
                )
        return requested_offering

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class PublicCallSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    customer_name = serializers.ReadOnlyField(source="manager.customer.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="manager.customer.uuid"
    )
    manager_uuid = serializers.UUIDField(read_only=True, source="manager.uuid")
    offerings = serializers.SerializerMethodField(method_name="get_offerings")
    rounds = serializers.SerializerMethodField()
    start_date = serializers.SerializerMethodField()
    end_date = serializers.SerializerMethodField()
    documents = CallDocumentSerializer(many=True, read_only=True)
    resource_templates = serializers.SerializerMethodField()
    fixed_duration_in_days = serializers.ReadOnlyField()
    description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    has_eligibility_restrictions = serializers.SerializerMethodField()

    class Meta:
        model = models.Call
        fields = (
            "url",
            "uuid",
            "created",
            "start_date",
            "end_date",
            "slug",
            "name",
            "description",
            "state",
            "manager",
            "manager_uuid",
            "customer_name",
            "customer_uuid",
            "offerings",
            "rounds",
            "documents",
            "resource_templates",
            "fixed_duration_in_days",
            "backend_id",
            "external_url",
            "reviewer_identity_visible_to_submitters",
            "reviews_visible_to_submitters",
            "has_eligibility_restrictions",
        )
        view_name = "proposal-public-call-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "manager": {
                "lookup_field": "uuid",
                "view_name": "call-managing-organisation-detail",
            },
            "created_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "documents": {"required": False},
            "reviewer_identity_visible_to_submitters": {"required": False},
            "reviews_visible_to_submitters": {"required": False},
        }

    def get_start_date(self, obj) -> datetime:
        first_round = obj.round_set.order_by("start_time").first()
        return first_round.start_time if first_round else None

    def get_end_date(self, obj) -> datetime:
        last_round = obj.round_set.order_by("-cutoff_time").first()
        return last_round.cutoff_time if last_round else None

    @extend_schema_field(NestedRequestedOfferingSerializer(many=True))
    def get_offerings(self, obj):
        queryset = obj.requestedoffering_set.filter(
            state=RequestedOfferingStates.ACCEPTED
        )
        serializer = NestedRequestedOfferingSerializer(
            queryset,
            many=True,
            read_only=True,
            context=self.context,
        )
        return serializer.data

    @extend_schema_field(NestedRoundSerializer(many=True))
    def get_rounds(self, obj):
        queryset = obj.round_set.all()
        all_open_rounds = queryset.filter(
            Q(start_time__lt=timezone.now()) & Q(cutoff_time__gt=timezone.now())
        )

        all_scheduled_rounds = queryset.filter(Q(start_time__gt=timezone.now()))

        all_closed_rounds = queryset.filter(Q(cutoff_time__lt=timezone.now()))

        sorted_queryset = (
            list(all_open_rounds) + list(all_scheduled_rounds) + list(all_closed_rounds)
        )
        serializer = NestedRoundSerializer(
            sorted_queryset,
            many=True,
            read_only=True,
            context=self.context,
        )
        return serializer.data

    @extend_schema_field(CallResourceTemplateSerializer(many=True))
    def get_resource_templates(self, obj):
        queryset = obj.resource_templates.all()
        serializer = CallResourceTemplateSerializer(
            queryset,
            many=True,
            read_only=True,
            context=self.context,
        )
        return serializer.data

    def get_has_eligibility_restrictions(self, obj) -> bool:
        """Check if call has any eligibility restrictions configured."""
        return bool(
            obj.user_nationalities
            or obj.user_organization_types
            or obj.user_assurance_levels
            or obj.user_email_patterns
            or obj.user_affiliations
            or obj.user_identity_sources
        )


class RequestedOfferingSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    NestedRequestedOfferingSerializer,
):
    url = serializers.SerializerMethodField()
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    approved_by_name = serializers.ReadOnlyField(source="approved_by.full_name")

    class Meta(NestedRequestedOfferingSerializer.Meta):
        fields = NestedRequestedOfferingSerializer.Meta.fields + [
            "url",
            "approved_by",
            "created_by",
            "created_by_name",
            "approved_by_name",
            "description",
        ]
        read_only_fields = (
            "created_by",
            "approved_by",
        )
        protected_fields = ("offering",)
        extra_kwargs = {
            **NestedRequestedOfferingSerializer.Meta.extra_kwargs,
            **{
                "approved_by": {
                    "lookup_field": "uuid",
                    "view_name": "user-detail",
                },
                "created_by": {
                    "lookup_field": "uuid",
                    "view_name": "user-detail",
                },
            },
        }

    def get_url(self, requested_offering) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-offering-detail",
                kwargs={
                    "uuid": requested_offering.call.uuid.hex,
                    "obj_uuid": requested_offering.uuid.hex,
                },
            )
        )

    def validate_offering(self, offering):
        user = self.context["request"].user

        if not (
            marketplace_models.Offering.objects.filter(id=offering.id)
            .filter_by_ordering_availability_for_user(user)
            .exists()
        ):
            raise serializers.ValidationError(
                {"offering": _("You do not have permissions for this offering.")}
            )

        return offering

    def validate_attributes(self, attributes):
        if not attributes:
            return {}

        return attributes

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class RequestedResourceSerializer(
    core_serializers.AugmentedSerializerMixin, NestedRequestedResourceSerializer
):
    requested_offering_uuid = serializers.UUIDField(write_only=True, required=False)
    call_resource_template_uuid = serializers.UUIDField(write_only=True, required=False)

    class Meta(NestedRequestedResourceSerializer.Meta):
        fields = NestedRequestedResourceSerializer.Meta.fields + [
            "requested_offering_uuid",
            "call_resource_template_uuid",
        ]

        read_only_fields = (
            "created_by",
            "resource",
        )

    def validate(self, attrs):
        if self.instance:
            return attrs

        proposal = attrs["proposal"]
        call = proposal.round.call

        # Handle resource template based requests
        call_resource_template_uuid = attrs.pop("call_resource_template_uuid", None)
        requested_offering_uuid = attrs.pop("requested_offering_uuid", None)

        if call_resource_template_uuid:
            # Creating from template
            try:
                template = call.resource_templates.get(uuid=call_resource_template_uuid)
            except models.CallResourceTemplate.DoesNotExist:
                raise serializers.ValidationError(
                    {"call_resource_template_uuid": _("Resource template not found.")}
                )

            attrs["call_resource_template"] = template
            attrs["requested_offering"] = template.requested_offering

            # Use template's attributes and limits as defaults
            if not attrs.get("attributes"):
                attrs["attributes"] = template.attributes
            if not attrs.get("limits"):
                attrs["limits"] = template.limits

        elif requested_offering_uuid:
            # Traditional direct offering request
            try:
                requested_offering = call.requestedoffering_set.get(
                    uuid=requested_offering_uuid
                )
            except models.RequestedOffering.DoesNotExist:
                raise serializers.ValidationError(
                    {
                        "requested_offering_uuid": _(
                            "Requested offering has not been found."
                        )
                    }
                )

            if requested_offering.state != RequestedOfferingStates.ACCEPTED:
                raise serializers.ValidationError(
                    _("Offering has not been confirmed by service provider.")
                )

            # Check if call has resource templates - if so, direct requests may not be allowed
            if call.resource_templates.exists():
                # Check if this offering is available through a template
                available_offering = call.resource_templates.filter(
                    requested_offering=requested_offering
                ).exists()
                if not available_offering:
                    raise serializers.ValidationError(
                        _(
                            "This offering is not available for direct requests. Please use a resource template."
                        )
                    )

            attrs["requested_offering"] = requested_offering
        else:
            raise serializers.ValidationError(
                _(
                    "Either requested_offering_uuid or call_resource_template_uuid must be provided."
                )
            )

        return attrs

    def validate_attributes(self, attributes):
        if not attributes:
            return {}

        return attributes

    def validate_proposal(self, proposal):
        if proposal.state != ProposalStates.DRAFT:
            raise serializers.ValidationError(
                _("Only proposals with a draft status are available for editing.")
            )

        return proposal

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class RequestedResourcePurchaseOrderSerializer(serializers.ModelSerializer):
    """Multipart write of the purchase order, mirroring OrderAttachmentSerializer.

    Kept off the main serializer because a file cannot ride along with the JSON
    body the resource form submits.
    """

    class Meta:
        model = models.RequestedResource
        fields = ("attachment", "purchase_order_reference")
        extra_kwargs = {
            "attachment": {"required": False, "allow_null": True},
            "purchase_order_reference": {"required": False, "allow_blank": True},
        }


class ProviderRequestedResourceSerializer(NestedRequestedResourceSerializer):
    proposal_name = serializers.ReadOnlyField(source="proposal.name")

    class Meta(NestedRequestedResourceSerializer.Meta):
        fields = NestedRequestedResourceSerializer.Meta.fields + [
            "proposal_name",
            "proposal",
        ]

        extra_kwargs = {
            **NestedRequestedResourceSerializer.Meta.extra_kwargs,
            **{
                "proposal": {
                    "lookup_field": "uuid",
                    "view_name": "proposal-proposal-detail",
                },
                "url": {
                    "lookup_field": "uuid",
                    "view_name": "proposal-requested-resource-detail",
                },
            },
        }


class ProviderRequestedOfferingSerializer(NestedRequestedOfferingSerializer):
    call_name = serializers.ReadOnlyField(source="call.name")
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_email = serializers.ReadOnlyField(source="created_by.email")

    class Meta(NestedRequestedOfferingSerializer.Meta):
        fields = NestedRequestedOfferingSerializer.Meta.fields + [
            "url",
            "call_name",
            "call",
            "description",
            "created_by_name",
            "created_by_email",
        ]
        read_only_fields = ("description", "created_by")
        extra_kwargs = {
            "approved_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "created_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-public-call-detail",
            },
            "plan": {
                "lookup_field": "uuid",
                "view_name": "marketplace-plan-detail",
                "read_only": True,
            },
            "url": {
                "lookup_field": "uuid",
                "view_name": "proposal-requested-offering-detail",
            },
        }


class CallApplicantVisibilityConfigSerializer(UserAttributeConfigBaseSerializer):
    class Meta(UserAttributeConfigBaseSerializer.Meta):
        model = models.CallApplicantVisibilityConfig


class ProtectedCallSerializer(PublicCallSerializer):
    reference_code = serializers.CharField(source="backend_id", required=False)
    fixed_duration_in_days = serializers.IntegerField(required=False, allow_null=True)
    reviewer_identity_visible_to_submitters = serializers.BooleanField(
        help_text="Whether proposal applicants can see reviewer identities",
        required=False,
    )
    reviews_visible_to_submitters = serializers.BooleanField(
        help_text="Whether proposal applicants can see review comments and scores",
        required=False,
    )
    compliance_checklist = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=checklist_models.Checklist.objects.filter(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        ),
        required=False,
        allow_null=True,
        help_text="Compliance checklist that proposals must complete before submission",
    )
    compliance_checklist_name = serializers.CharField(
        source="compliance_checklist.name", read_only=True
    )
    proposal_slug_template = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=(
            "Template for proposal slugs. Supports: {call_slug}, {round_slug}, "
            "{org_slug}, {year}, {month}, {counter}, {counter_padded}. "
            "Default: {round_slug}-{counter_padded}"
        ),
    )

    # Eligibility restriction fields (from UserDetailsMatchMixin)
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of email regex patterns. User must match one.",
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed affiliations. User must have one.",
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed identity sources (identity providers).",
    )
    user_nationalities = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed nationality codes (ISO 3166-1 alpha-2). User must have one.",
    )
    user_organization_types = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed organization type URNs (SCHAC). User must match one.",
    )
    user_assurance_levels = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of required assurance URIs (REFEDS). User must have ALL of these.",
    )

    applicant_visibility_config = CallApplicantVisibilityConfigSerializer(
        required=False,
        allow_null=True,
    )

    has_proposals = serializers.SerializerMethodField(
        help_text="Whether any proposal has been submitted to this call. "
        "Used by the frontend to gate slug-template and checklist fields."
    )

    @extend_schema_field(serializers.BooleanField())
    def get_has_proposals(self, obj) -> bool:
        return models.Proposal.objects.filter(round__call=obj).exists()

    class Meta(PublicCallSerializer.Meta):
        fields = PublicCallSerializer.Meta.fields + (
            "created_by",
            "reference_code",
            "compliance_checklist",
            "compliance_checklist_name",
            "proposal_slug_template",
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
            "user_nationalities",
            "user_organization_types",
            "user_assurance_levels",
            "applicant_visibility_config",
            "has_proposals",
        )
        view_name = "proposal-protected-call-detail"
        protected_fields = ("manager",)

    def validate_manager(self, manager: models.CallManagingOrganisation):
        user = self.context["request"].user

        if (
            manager
            and not user.is_staff
            and not permissions_utils.has_permission(
                user, permissions_enums.PermissionEnum.CREATE_CALL_PERMISSION, manager
            )
        ):
            raise serializers.ValidationError(
                "Current user does not belong to the selected organisation."
            )

        return manager

    def validate_compliance_checklist(self, value):
        """Prevent changing compliance checklist if proposals exist."""
        call: models.Call = self.instance
        if call and models.Proposal.objects.filter(round__call=call).exists():
            if value != call.compliance_checklist:
                raise serializers.ValidationError(
                    "Cannot change compliance checklist when proposals exist"
                )
        return value

    def validate_proposal_slug_template(self, value):
        """Validate that the template only uses allowed placeholders."""
        if not value:
            return value

        # Extract all placeholders from the template (handle format specs like {counter:03d})
        placeholders = re.findall(r"\{([^}:]+)", value)

        allowed_placeholders = {
            "call_slug",
            "round_slug",
            "org_slug",
            "year",
            "month",
            "counter",
            "counter_padded",
        }

        invalid_placeholders = set(placeholders) - allowed_placeholders

        if invalid_placeholders:
            raise serializers.ValidationError(
                f"Invalid placeholders: {', '.join(sorted(invalid_placeholders))}. "
                f"Allowed: {', '.join(sorted(allowed_placeholders))}"
            )

        # Validate the template by attempting a test format
        test_context = {
            "call_slug": "TEST-CALL",
            "round_slug": "TEST-ROUND-202401",
            "org_slug": "TEST-ORG",
            "year": "2024",
            "month": "01",
            "counter": "1",
            "counter_padded": "001",
        }

        try:
            value.format(**test_context)
        except (KeyError, ValueError) as e:
            raise serializers.ValidationError(f"Invalid template format: {e}")

        # Prevent changing template if proposals exist
        call: models.Call = self.instance
        if call and models.Proposal.objects.filter(round__call=call).exists():
            if value != call.proposal_slug_template:
                raise serializers.ValidationError(
                    "Cannot change proposal slug template when proposals exist"
                )

        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return data
        if data.get("applicant_visibility_config") is None:
            synthetic = models.CallApplicantVisibilityConfig(call=instance)
            data["applicant_visibility_config"] = (
                CallApplicantVisibilityConfigSerializer(
                    synthetic, context=self.context
                ).data
            )
        return data

    def create(self, validated_data):
        request = self.context["request"]
        customer = validated_data.get("manager", None).customer
        if not permissions_utils.has_permission(
            request,
            permissions_enums.PermissionEnum.CREATE_CALL,
            customer.callmanagingorganisation,
        ):
            raise PermissionDenied()

        validated_data["created_by"] = request.user
        has_visibility = "applicant_visibility_config" in validated_data
        visibility_data = validated_data.pop("applicant_visibility_config", None)
        call = super().create(validated_data)
        if has_visibility and visibility_data is not None:
            seed = models.CallApplicantVisibilityConfig.get_default_exposure_flags()
            models.CallApplicantVisibilityConfig.objects.create(
                call=call, **{**seed, **visibility_data}
            )
        return call

    def update(self, instance, validated_data):
        if "fixed_duration_in_days" in validated_data:
            fixed_duration_in_days = validated_data["fixed_duration_in_days"]
            proposals = models.Proposal.objects.filter(
                round__call=instance,
                state__in=[ProposalStates.DRAFT, ProposalStates.IN_REVIEW],
            )
            for proposal in proposals:
                proposal.duration_in_days = fixed_duration_in_days
                proposal.save()

        has_visibility = "applicant_visibility_config" in validated_data
        visibility_data = validated_data.pop("applicant_visibility_config", None)
        call = super().update(instance, validated_data)
        if has_visibility:
            if visibility_data is None:
                models.CallApplicantVisibilityConfig.objects.filter(call=call).delete()
            elif (
                existing := models.CallApplicantVisibilityConfig.objects.filter(
                    call=call
                ).first()
            ) is not None:
                # Row exists — partial PATCH only touches supplied keys.
                for key, value in visibility_data.items():
                    setattr(existing, key, value)
                existing.save()
            else:
                # First create — seed Constance defaults so unspecified fields
                # don't silently fall back to model defaults.
                seed = models.CallApplicantVisibilityConfig.get_default_exposure_flags()
                models.CallApplicantVisibilityConfig.objects.create(
                    call=call, **{**seed, **visibility_data}
                )
        return call


class ProtectedRoundSerializer(
    core_serializers.AugmentedSerializerMixin, NestedRoundSerializer
):
    url = serializers.SerializerMethodField()
    proposals = ProtectedProposalListSerializer(
        many=True, read_only=True, source="proposal_set"
    )
    review_duration_in_days = serializers.IntegerField(required=False)

    class Meta(NestedRoundSerializer.Meta):
        fields = NestedRoundSerializer.Meta.fields + ["url", "proposals"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")

        # Only allow staff to edit slug field
        if request and not (request.user and request.user.is_staff):
            if "slug" in fields:
                fields["slug"].read_only = True

        return fields

    def get_url(self, call_round) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-round-detail",
                kwargs={
                    "uuid": call_round.call.uuid.hex,
                    "obj_uuid": call_round.uuid.hex,
                },
            )
        )

    def create(self, validated_data):
        # Set a default value if not provided by the user
        if "review_duration_in_days" not in validated_data:
            validated_data["review_duration_in_days"] = config.PROPOSAL_REVIEW_DURATION

        return super().create(validated_data)

    def validate(self, attrs):
        start_time = attrs.get("start_time")
        cutoff_time = attrs.get("cutoff_time")

        if start_time and cutoff_time and cutoff_time <= start_time:
            raise serializers.ValidationError(
                {"start_time": _("Cutoff time must be later than start time.")}
            )

        call = self.context["view"].get_object()

        if (
            models.Round.objects.filter(
                call=call, start_time__lt=cutoff_time, cutoff_time__gt=start_time
            )
            .exclude(uuid=getattr(self.instance, "uuid", None))
            .exists()
        ):
            raise serializers.ValidationError(
                "Round is overlapping with another round."
            )

        return attrs


class ProposalDocumentationSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = serializers.IntegerField(source="file.size", read_only=True)

    class Meta:
        model = models.ProposalDocumentation
        fields = ["uuid", "file", "file_name", "file_size", "created"]
        read_only_fields = ["uuid"]


class ProposalDetachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.UUIDField())


class ProposalUpdateProjectDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Proposal
        fields = [
            "name",
            "description",
            "project_summary",
            "project_is_confidential",
            "project_has_civilian_purpose",
            "duration_in_days",
            "oecd_fos_2007_code",
        ]


class ProposalComplianceStatusSerializer(serializers.Serializer):
    error = serializers.CharField(required=False)
    has_checklist = serializers.BooleanField()
    is_completed = serializers.BooleanField()
    requires_review = serializers.BooleanField()
    completion_percentage = serializers.IntegerField()
    reviewed_by = serializers.CharField(allow_null=True)
    reviewed_at = serializers.DateTimeField(allow_null=True)
    checklist_name = serializers.CharField(required=False)
    unanswered_required_count = serializers.IntegerField(required=False)


class ProposalCanSubmitResponseSerializer(serializers.Serializer):
    can_submit = serializers.BooleanField()
    error = serializers.CharField(allow_null=True)


class ProposalSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round = NestedRoundSerializer(read_only=True)
    round_uuid = serializers.UUIDField(write_only=True, required=True)
    call_uuid = serializers.UUIDField(source="round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="round.call.name", read_only=True)
    call_managing_organisation_uuid = serializers.ReadOnlyField(
        source="round.call.manager.uuid"
    )
    supporting_documentation = ProposalDocumentationSerializer(
        many=True, read_only=True, source="proposaldocumentation_set"
    )
    oecd_fos_2007_label = serializers.CharField(
        read_only=True, source="get_oecd_fos_2007_code_display"
    )
    science_sub_domain = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.ScienceSubDomain.objects.all(),
        allow_null=True,
        required=False,
    )
    science_sub_domain_name = serializers.ReadOnlyField(
        source="science_sub_domain.name",
    )
    science_domain_uuid = serializers.ReadOnlyField(
        source="science_sub_domain.domain.uuid",
    )
    science_domain_name = serializers.ReadOnlyField(
        source="science_sub_domain.domain.name",
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_uuid = serializers.UUIDField(source="created_by.uuid", read_only=True)
    project_name = serializers.ReadOnlyField(source="project.name")
    description = core_serializers.HTMLCleanField(required=False, allow_blank=True)

    # Applicant attributes — gated by CallApplicantVisibilityConfig for reviewers.
    applicant_username = serializers.ReadOnlyField(source="created_by.username")
    applicant_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    applicant_first_name = serializers.ReadOnlyField(source="created_by.first_name")
    applicant_last_name = serializers.ReadOnlyField(source="created_by.last_name")
    applicant_email = serializers.ReadOnlyField(source="created_by.email")
    applicant_registration_method = serializers.ReadOnlyField(
        source="created_by.registration_method"
    )
    applicant_phone_number = serializers.ReadOnlyField(source="created_by.phone_number")
    applicant_organization = serializers.ReadOnlyField(source="created_by.organization")
    applicant_organization_country = serializers.ReadOnlyField(
        source="created_by.organization_country"
    )
    applicant_organization_type = serializers.ReadOnlyField(
        source="created_by.organization_type"
    )
    applicant_organization_registry_code = serializers.ReadOnlyField(
        source="created_by.organization_registry_code"
    )
    applicant_organization_vat_code = serializers.ReadOnlyField(
        source="created_by.organization_vat_code"
    )
    applicant_organization_address = serializers.ReadOnlyField(
        source="created_by.organization_address",
        allow_null=True,
    )
    applicant_job_title = serializers.ReadOnlyField(source="created_by.job_title")
    applicant_affiliations = serializers.ListField(
        child=serializers.CharField(),
        source="created_by.affiliations",
        read_only=True,
    )
    applicant_gender = serializers.ReadOnlyField(source="created_by.gender")
    applicant_personal_title = serializers.ReadOnlyField(
        source="created_by.personal_title"
    )
    applicant_place_of_birth = serializers.ReadOnlyField(
        source="created_by.place_of_birth"
    )
    applicant_address = serializers.ReadOnlyField(source="created_by.address")
    applicant_country_of_residence = serializers.ReadOnlyField(
        source="created_by.country_of_residence"
    )
    applicant_nationality = serializers.ReadOnlyField(source="created_by.nationality")
    applicant_nationalities = serializers.ListField(
        child=serializers.CharField(), source="created_by.nationalities", read_only=True
    )
    applicant_eduperson_assurance = serializers.ListField(
        child=serializers.CharField(),
        source="created_by.eduperson_assurance",
        read_only=True,
    )
    applicant_identity_source = serializers.ReadOnlyField(
        source="created_by.identity_source"
    )
    applicant_civil_number = serializers.ReadOnlyField(source="created_by.civil_number")
    applicant_birth_date = serializers.ReadOnlyField(source="created_by.birth_date")
    applicant_active_isds = serializers.ListField(
        child=serializers.CharField(), source="created_by.active_isds", read_only=True
    )

    # Compliance fields
    compliance_status = serializers.SerializerMethodField()
    can_submit = serializers.SerializerMethodField()
    awaiting_manual_advance = serializers.SerializerMethodField()

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
            "url",
            "slug",
            "name",
            "description",
            "project_name",
            "project_summary",
            "project_is_confidential",
            "project_has_civilian_purpose",
            "supporting_documentation",
            "state",
            "approved_by",
            "created_by",
            "created_by_name",
            "created_by_uuid",
            # Applicant attributes (gated by CallApplicantVisibilityConfig)
            "applicant_username",
            "applicant_full_name",
            "applicant_first_name",
            "applicant_last_name",
            "applicant_email",
            "applicant_registration_method",
            "applicant_phone_number",
            "applicant_organization",
            "applicant_organization_country",
            "applicant_organization_type",
            "applicant_organization_registry_code",
            "applicant_organization_vat_code",
            "applicant_organization_address",
            "applicant_job_title",
            "applicant_affiliations",
            "applicant_gender",
            "applicant_personal_title",
            "applicant_place_of_birth",
            "applicant_address",
            "applicant_country_of_residence",
            "applicant_nationality",
            "applicant_nationalities",
            "applicant_eduperson_assurance",
            "applicant_identity_source",
            "applicant_civil_number",
            "applicant_birth_date",
            "applicant_active_isds",
            "duration_in_days",
            "project",
            "round",
            "round_uuid",
            "call_uuid",
            "call_name",
            "call_managing_organisation_uuid",
            "oecd_fos_2007_code",
            "oecd_fos_2007_label",
            "science_sub_domain",
            "science_sub_domain_name",
            "science_domain_uuid",
            "science_domain_name",
            "allocation_comment",
            "created",
            "compliance_status",
            "can_submit",
            "awaiting_manual_advance",
            "workflow_step",
        ]
        read_only_fields = (
            "workflow_step",
            "created_by",
            "approved_by",
            "project",
            "allocation_comment",
        )
        protected_fields = ("round_uuid",)
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "slug": {"required": False},
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "approved_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "project": {"lookup_field": "uuid", "view_name": "project-detail"},
        }

    def validate_name(self, value):
        # The proposal name is the applicant-controlled part of the project name
        # created on approval (call_prefix - date - name), so hold it to the same
        # configurable pattern as the main project API. Validated on both create
        # and rename since the field-level check runs regardless of ``validate``.
        error = get_project_name_regex_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate(self, attrs):
        if self.instance:
            return attrs

        round_uuid = attrs.pop("round_uuid")

        try:
            call_round = models.Round.objects.get(uuid=round_uuid)
        except models.Round.DoesNotExist:
            raise serializers.ValidationError({"round_uuid": _("Round not found.")})

        if call_round.call.state != CallStates.ACTIVE:
            raise serializers.ValidationError(_("Call is not active."))

        # A proposal exists only while its round is open — it cannot be drafted
        # ahead of one opening, and cannot be started after the cutoff. Same
        # rule as submission, so a proposal can never be created into a state it
        # could not then be sent from.
        if call_round.status == RoundStatuses.SCHEDULED:
            raise serializers.ValidationError(
                _("Round has not opened yet, so a proposal cannot be created.")
            )
        if call_round.status == RoundStatuses.ENDED:
            raise serializers.ValidationError(
                _("Round has closed, so a proposal can no longer be created.")
            )

        attrs["round"] = call_round
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        proposal = super().create(validated_data)

        # Set fixed duration if specified by call
        if proposal.round.call.fixed_duration_in_days:
            proposal.duration_in_days = proposal.round.call.fixed_duration_in_days
            proposal.save()

        return proposal

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return data
        request = self.context.get("request")
        user = request.user if request else None
        # ``approved_by`` names the decision-maker (the call manager / staff who
        # accepted the proposal). Honour the call's blind-review setting: hide
        # it from the proposal's own submitter unless the call reveals reviewer
        # identity to submitters. Call team and staff (not the submitter) keep
        # seeing it.
        if (
            user is not None
            and not getattr(user, "is_staff", False)
            and instance.created_by_id == getattr(user, "id", None)
            and not instance.round.call.reviewer_identity_visible_to_submitters
        ):
            data["approved_by"] = None
        return filter_applicant_fields_for_reviewer(data, instance, user)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")

        # Only allow staff to edit slug field
        if request and not (request.user and request.user.is_staff):
            if "slug" in fields:
                fields["slug"].read_only = True

        # Make duration_in_days read-only if call has fixed duration
        def is_fixed_duration(instance):
            try:
                return instance.round.call.fixed_duration_in_days
            except AttributeError:
                return False

        # Handle both single instance and list
        instances = (
            self.instance
            if isinstance(self.instance, (list | tuple))
            else [self.instance]
            if self.instance
            else []
        )

        if any(is_fixed_duration(obj) for obj in instances):
            fields["duration_in_days"].read_only = True
        elif hasattr(self, "initial_data") and "round_uuid" in self.initial_data:
            # For creation, check if the call has fixed duration
            try:
                round_uuid = self.initial_data["round_uuid"]
                call_round = models.Round.objects.get(uuid=round_uuid)
                if call_round.call.fixed_duration_in_days:
                    fields["duration_in_days"].read_only = True
            except (models.Round.DoesNotExist, KeyError):
                pass

        return fields

    @extend_schema_field(ProposalComplianceStatusSerializer(allow_null=True))
    def get_compliance_status(self, obj):
        """Get compliance checklist status."""
        if not obj.round.call.compliance_checklist:
            return None

        if not hasattr(obj, "checklist_completion"):
            return {
                "error": "Compliance checklist not initialized",
                "has_checklist": True,
                "is_completed": False,
                "requires_review": False,
                "completion_percentage": 0,
            }

        completion = obj.checklist_completion
        return {
            "has_checklist": True,
            "is_completed": completion.is_completed,
            "requires_review": completion.requires_review,
            "completion_percentage": completion.get_completion_percentage(),
            "reviewed_by": completion.reviewed_by.full_name
            if completion.reviewed_by
            else None,
            "reviewed_at": completion.reviewed_at,
            "checklist_name": completion.checklist.name,
            "unanswered_required_count": completion.get_unanswered_required_questions().count(),
        }

    @extend_schema_field(ProposalCanSubmitResponseSerializer)
    def get_can_submit(self, obj):
        """Get whether proposal can be submitted."""
        can_submit, error = obj.can_submit()
        return {"can_submit": can_submit, "error": error}

    @extend_schema_field(serializers.BooleanField())
    def get_awaiting_manual_advance(self, obj) -> bool:
        """True iff the current step is completed and awaiting a manual advance.

        Prefers the queryset annotations (set by ProposalViewSet.get_queryset)
        to avoid a per-row query on list. Falls back to the DB-fresh service
        helper for objects serialized outside that viewset (e.g. after a
        mutation).
        """
        if hasattr(obj, "_awaiting_manual_step"):
            return bool(
                obj.workflow_step
                and obj._awaiting_manual_step
                and obj._latest_step_status == WorkflowStepInstanceStatuses.COMPLETED
            )
        return workflow_service.is_awaiting_manual_advance(obj)


class RoundReviewerSerializer(serializers.Serializer):
    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    accepted_proposals = serializers.IntegerField()
    rejected_proposals = serializers.IntegerField()
    in_review_proposals = serializers.IntegerField()

    def get_full_name(self, obj) -> str:
        return f"{obj.first_name} {obj.last_name}"


class CallRoundSerializer(serializers.HyperlinkedModelSerializer):
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.Round
        fields = [
            "url",
            "uuid",
            "slug",
            "start_time",
            "cutoff_time",
            "call_uuid",
            "call_name",
            "status",
        ]
        extra_kwargs = {
            "slug": {"required": False},
            "url": {
                "lookup_field": "uuid",
                "view_name": "call-round-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-public-call-detail",
            },
        }


class CallManagingOrganisationStatSerializer(serializers.Serializer):
    open_calls = serializers.IntegerField(read_only=True)
    active_rounds = serializers.IntegerField(read_only=True)
    accepted_proposals = serializers.IntegerField(read_only=True)
    pending_proposals = serializers.IntegerField(read_only=True)
    pending_review = serializers.IntegerField(read_only=True)
    rounds_closing_in_one_week = serializers.IntegerField(read_only=True)
    calls_closing_in_one_week = serializers.IntegerField(read_only=True)
    offering_requests_pending = serializers.IntegerField(read_only=True)


class CallPerformanceStatSerializer(serializers.Serializer):
    call_uuid = serializers.UUIDField(read_only=True)
    call_name = serializers.CharField(read_only=True)
    managing_organization_name = serializers.CharField(read_only=True)
    state = serializers.CharField(read_only=True)
    total_proposals = serializers.IntegerField(read_only=True)
    proposals_draft = serializers.IntegerField(read_only=True)
    proposals_submitted = serializers.IntegerField(read_only=True)
    proposals_in_review = serializers.IntegerField(read_only=True)
    proposals_accepted = serializers.IntegerField(read_only=True)
    proposals_rejected = serializers.IntegerField(read_only=True)
    proposals_canceled = serializers.IntegerField(read_only=True)
    acceptance_rate = serializers.FloatField(read_only=True)
    total_reviews = serializers.IntegerField(read_only=True)
    reviews_completed = serializers.IntegerField(read_only=True)
    average_score = serializers.FloatField(read_only=True, allow_null=True)
    active_rounds = serializers.IntegerField(read_only=True)
    last_submission_date = serializers.DateField(read_only=True, allow_null=True)


class ReviewProgressStatSerializer(serializers.Serializer):
    reviewer_uuid = serializers.UUIDField(read_only=True)
    reviewer_name = serializers.CharField(read_only=True)
    reviewer_email = serializers.EmailField(read_only=True)
    total_assigned = serializers.IntegerField(read_only=True)
    pending = serializers.IntegerField(read_only=True)
    in_progress = serializers.IntegerField(read_only=True)
    completed = serializers.IntegerField(read_only=True)
    declined = serializers.IntegerField(read_only=True)
    average_score = serializers.FloatField(read_only=True, allow_null=True)
    average_review_time_days = serializers.FloatField(read_only=True, allow_null=True)
    completion_rate = serializers.FloatField(read_only=True)


class ResourceDemandStatSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(read_only=True)
    offering_name = serializers.CharField(read_only=True)
    offering_type = serializers.CharField(read_only=True)
    provider_name = serializers.CharField(read_only=True)
    proposal_count = serializers.IntegerField(read_only=True)
    request_count = serializers.IntegerField(read_only=True)
    approved_count = serializers.IntegerField(read_only=True)
    pending_count = serializers.IntegerField(read_only=True)
    total_requested_limits = serializers.DictField(
        child=serializers.FloatField(), read_only=True
    )
    total_approved_limits = serializers.DictField(
        child=serializers.FloatField(), read_only=True
    )


class CallAttachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.FileField())
    description = serializers.CharField(required=False)


class CallDetachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.UUIDField())


class ProposalProjectRoleMappingSerializer(serializers.HyperlinkedModelSerializer):
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    proposal_role = serializers.SlugRelatedField(
        slug_field="name",
        queryset=Role.objects.filter(is_active=True, content_type__model="proposal"),
        required=True,
        allow_null=False,
    )
    project_role = serializers.SlugRelatedField(
        slug_field="name",
        queryset=Role.objects.filter(is_active=True, content_type__model="project"),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.ProposalProjectRoleMapping
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "proposal_role",
            "project_role",
        ]
        read_only_fields = ("uuid",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "call-proposal-project-role-mapping-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }

    def validate(self, attrs):
        if self.instance:
            call = self.instance.call
        else:
            call = attrs["call"]
        if not permissions_utils.has_permission(
            self.context["request"],
            permissions_enums.PermissionEnum.UPDATE_CALL,
            call,
        ):
            raise PermissionDenied()

        if call.state == CallStates.ARCHIVED:
            raise serializers.ValidationError(_("Cannot modify an archived call."))

        return attrs

    def get_fields(self):
        fields = super().get_fields()
        # Make only project_role updatable if instance exists
        if hasattr(self, "instance") and self.instance:
            fields["proposal_role"].read_only = True
            fields["call"].read_only = True
        return fields


# Checklist Integration Serializers
# Backward compatibility aliases - use generic serializers from checklist app
ProposalChecklistCompletionSerializer = (
    checklist_serializers.ChecklistCompletionSerializer
)
ProposalChecklistAnswerSubmitSerializer = checklist_serializers.AnswerSubmitSerializer


class ProposalChecklistAnswerSubmitResponseSerializer(serializers.Serializer):
    """Custom response serializer for proposal answer submission that includes review status."""

    detail = serializers.CharField()
    completion = checklist_serializers.ChecklistCompletionReviewerSerializer()


class TechnicalAssessmentAnswerSerializer(serializers.Serializer):
    """One reviewer's answer to a technical-assessment question, with a
    human-readable ``answer_display`` (option labels for select questions)."""

    question_uuid = serializers.UUIDField(source="question.uuid")
    question_description = serializers.CharField(source="question.description")
    question_type = serializers.CharField(source="question.question_type")
    answer_data = serializers.JSONField()
    answer_display = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_answer_display(self, obj):
        question = obj.question
        value = obj.answer_data
        if question.question_type in ("single_select", "multi_select"):
            option_uuids = value if isinstance(value, list) else [value]
            labels = list(
                checklist_models.QuestionOption.objects.filter(
                    question=question, uuid__in=[str(u) for u in option_uuids]
                )
                .order_by("order")
                .values_list("label", flat=True)
            )
            return ", ".join(labels) if labels else None
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        if value in (None, ""):
            return None
        return str(value)


class StepChecklistResponseGroupSerializer(serializers.Serializer):
    """All answers a single reviewer gave to a step's checklist (grouped),
    for the threaded technical-assessment display (WAL-9337).

    Reviewer identity (uuid, name, image) is anonymized when the **applicant**
    is viewing and the call's ``reviewer_identity_visible_to_submitters`` is
    off — the decision + comment stay visible, but who said it is hidden.
    Managers, staff and offering managers always see identities.
    """

    user_uuid = serializers.SerializerMethodField()
    user_full_name = serializers.SerializerMethodField()
    user_image = serializers.SerializerMethodField()
    submitted_at = serializers.DateTimeField(allow_null=True)
    answers = TechnicalAssessmentAnswerSerializer(many=True)

    def _anonymize(self):
        request = self.context.get("request")
        proposal = self.context.get("proposal")
        if not request or proposal is None:
            return False
        user = request.user
        if user.is_staff or proposal.created_by_id != user.id:
            return False
        return not proposal.round.call.reviewer_identity_visible_to_submitters

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_user_uuid(self, obj):
        return None if self._anonymize() else obj["user"].uuid

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_full_name(self, obj):
        if self._anonymize():
            return str(_("Technical reviewer"))
        return obj["user"].full_name

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_image(self, obj):
        if self._anonymize():
            return None
        image = getattr(obj["user"], "image", None)
        if not image:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(image.url) if request else image.url


# Response serializer is now handled generically by ChecklistResponseSerializer
# Keep this for backward compatibility in call manager views
ProposalComplianceChecklistResponseSerializer = (
    checklist_serializers.ChecklistResponseSerializer
)


class CallComplianceChecklistInfoSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField()
    total_questions = serializers.IntegerField()
    required_questions = serializers.IntegerField()


class CallComplianceOverviewProposalReviewTriggerSerializer(serializers.Serializer):
    question = serializers.CharField()
    answer = serializers.JSONField()
    trigger_value = serializers.JSONField()
    operator = serializers.CharField()


class CallComplianceOverviewProposalComplianceSerializer(serializers.Serializer):
    is_completed = serializers.BooleanField()
    requires_review = serializers.BooleanField()
    completion_percentage = serializers.IntegerField()
    reviewed_by = serializers.CharField(allow_null=True)
    reviewed_at = serializers.DateTimeField(allow_null=True)
    review_triggers = CallComplianceOverviewProposalReviewTriggerSerializer(many=True)
    unanswered_required_count = serializers.IntegerField()


class CallComplianceOverviewProposalSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    state = serializers.CharField()
    created_by = serializers.CharField(allow_null=True)
    created_by_uuid = serializers.UUIDField(allow_null=True)
    compliance = CallComplianceOverviewProposalComplianceSerializer(allow_null=True)


class CallComplianceOverviewSerializer(serializers.Serializer):
    """Serializer for call manager compliance overview."""

    checklist = serializers.SerializerMethodField()
    proposals = serializers.SerializerMethodField()

    @extend_schema_field(CallComplianceChecklistInfoSerializer(allow_null=True))
    def get_checklist(self, call):
        """Get checklist information."""
        if not call.compliance_checklist:
            return None

        return {
            "uuid": str(call.compliance_checklist.uuid),
            "name": call.compliance_checklist.name,
            "description": call.compliance_checklist.description,
            "total_questions": call.compliance_checklist.questions.count(),
            "required_questions": call.compliance_checklist.questions.filter(
                required=True
            ).count(),
        }

    @extend_schema_field(CallComplianceOverviewProposalSerializer(many=True))
    def get_proposals(self, call):
        """Get proposal compliance status."""
        proposals_data = []

        for proposal in models.Proposal.objects.filter(round__call=call):
            proposal_data = {
                "uuid": str(proposal.uuid),
                "name": proposal.name,
                "state": proposal.state,
                "created_by": proposal.created_by.full_name
                if proposal.created_by
                else None,
                "created_by_uuid": str(proposal.created_by.uuid)
                if proposal.created_by
                else None,
                "compliance": None,
            }

            # Add compliance information if exists
            if hasattr(proposal, "checklist_completion"):
                completion = proposal.checklist_completion
                if completion:
                    proposal_data["compliance"] = {
                        "is_completed": completion.is_completed,
                        "requires_review": completion.requires_review,
                        "completion_percentage": completion.get_completion_percentage(),
                        "reviewed_by": completion.reviewed_by.full_name
                        if completion.reviewed_by
                        else None,
                        "reviewed_at": completion.reviewed_at,
                        "review_triggers": completion.get_review_trigger_summary(),
                        "unanswered_required_count": completion.get_unanswered_required_questions().count(),
                    }

            proposals_data.append(proposal_data)

        return proposals_data


class AvailableChecklistSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    checklist_type = serializers.CharField(read_only=True)
    questions_count = serializers.SerializerMethodField()
    category_name = serializers.CharField(read_only=True, allow_null=True)
    category_uuid = serializers.UUIDField(read_only=True, allow_null=True)

    @extend_schema_field(serializers.IntegerField())
    def get_questions_count(self, obj):
        return obj.questions.count()


class CallComplianceReviewSerializer(serializers.Serializer):
    """Serializer for call manager to review proposal compliance."""

    proposal_uuid = serializers.UUIDField()
    review_notes = serializers.CharField(required=False, allow_blank=True)

    def validate_proposal_uuid(self, value):
        """Validate that proposal belongs to the call."""
        call = self.context.get("call")
        if not call:
            raise serializers.ValidationError("Call context is required")

        try:
            proposal: models.Proposal = models.Proposal.objects.get(
                round__call=call, uuid=value
            )
            if not hasattr(proposal, "checklist_completion"):
                raise serializers.ValidationError(
                    "Proposal has no compliance checklist"
                )
            return value
        except models.Proposal.DoesNotExist:
            raise serializers.ValidationError("Proposal not found in this call")


# =============================================================================
# Reviewer Profile Serializers
# =============================================================================


class ReviewerStatsSerializer(serializers.HyperlinkedModelSerializer):
    """Read-only serializer for reviewer statistics."""

    class Meta:
        model = models.ReviewerStats
        fields = [
            "uuid",
            "total_reviews_completed",
            "total_reviews_declined",
            "total_reviews_timeout",
            "average_review_time_days",
            "average_score_given",
            "last_review_date",
            "quality_rating",
            "quality_rating_count",
        ]
        read_only_fields = fields


class ReviewerAffiliationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for reviewer affiliations."""

    organization_name_display = serializers.SerializerMethodField()
    organization = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Customer.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.ReviewerAffiliation
        fields = [
            "uuid",
            "organization",
            "organization_name",
            "organization_name_display",
            "organization_identifier",
            "department",
            "position_title",
            "start_date",
            "end_date",
            "is_primary",
            "affiliation_type",
            "created",
        ]

    def get_organization_name_display(self, obj) -> str:
        """Return organization name from linked Customer or from the text field."""
        if obj.organization:
            return obj.organization.name
        return obj.organization_name

    def validate(self, attrs):
        """Ensure at least organization or organization_name is provided."""
        organization = attrs.get("organization")
        organization_name = attrs.get("organization_name")

        if not organization and not organization_name:
            raise serializers.ValidationError(
                _("Either organization or organization_name must be provided.")
            )

        # If organization is linked, copy name for consistency
        if organization and not organization_name:
            attrs["organization_name"] = organization.name

        return attrs


class ExpertiseCategorySerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for expertise categories (taxonomy)."""

    class Meta:
        model = models.ExpertiseCategory
        fields = [
            "url",
            "uuid",
            "name",
            "code",
            "description",
            "parent",
            "level",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "parent": {
                "lookup_field": "uuid",
                "view_name": "expertise-category-detail",
                "required": False,
                "allow_null": True,
            },
        }


class ReviewerExpertiseSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for reviewer expertise keywords."""

    expertise_category_name = serializers.ReadOnlyField(
        source="expertise_category.name"
    )
    expertise_category = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ExpertiseCategory.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.ReviewerExpertise
        fields = [
            "uuid",
            "expertise_keyword",
            "expertise_category",
            "expertise_category_name",
            "proficiency_level",
            "years_experience",
            "last_active_date",
            "created",
        ]


class ReviewerPublicationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for reviewer publications."""

    # Declared explicitly so the schema renders an array; a bare JSONField is
    # mapped to a free-form object by JSONFieldExtension. The child is left
    # unconstrained because entries are {"name": ..., "orcid": ...} objects,
    # and plain name strings are still accepted for legacy records.
    coauthors = serializers.ListField(
        required=False,
        help_text=_("List of co-author names and identifiers"),
    )

    class Meta:
        model = models.ReviewerPublication
        fields = [
            "uuid",
            "title",
            "doi",
            "publication_year",
            "venue",
            "venue_type",
            "abstract",
            "coauthors",
            "external_ids",
            "is_excluded_from_matching",
            "created",
        ]


class ReviewerProfileSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Full serializer for reviewer profiles with nested relationships."""

    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_email = serializers.ReadOnlyField(source="user.email")
    user_uuid = serializers.UUIDField(source="user.uuid", read_only=True)
    affiliations = ReviewerAffiliationSerializer(many=True, read_only=True)
    expertise_set = ReviewerExpertiseSerializer(many=True, read_only=True)
    # Declared explicitly so the schema renders an array; a bare JSONField is
    # mapped to a free-form object by JSONFieldExtension.
    alternative_names = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_("List of name variants used in publications"),
    )
    publications = ReviewerPublicationSerializer(many=True, read_only=True)
    stats = ReviewerStatsSerializer(read_only=True)
    orcid_connected = serializers.SerializerMethodField()
    profile_completeness = serializers.SerializerMethodField()

    class Meta:
        model = models.ReviewerProfile
        fields = [
            "url",
            "uuid",
            "user",
            "user_uuid",
            "user_full_name",
            "user_email",
            "orcid_id",
            "orcid_connected",
            "orcid_last_sync",
            "biography",
            "alternative_names",
            "affiliations",
            "expertise_set",
            "publications",
            "stats",
            "profile_completeness",
            "is_published",
            "published_at",
            "available_for_reviews",
            "created",
            "modified",
        ]
        read_only_fields = [
            "user",
            "orcid_last_sync",
            "is_published",
            "published_at",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "user": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_orcid_connected(self, obj) -> bool:
        """Check if ORCID is connected (has access token)."""
        return bool(obj.orcid_access_token)

    def get_profile_completeness(self, obj) -> dict:
        """Calculate profile completeness percentage and missing fields."""
        checks = {
            "has_biography": bool(obj.biography),
            "has_orcid": bool(obj.orcid_id),
            "has_affiliations": obj.affiliations.exists(),
            "has_expertise": obj.expertise_set.exists(),
            "has_publications": obj.publications.exists(),
        }
        completed = sum(checks.values())
        total = len(checks)
        return {
            "percentage": round(completed / total * 100) if total else 0,
            "completed_checks": completed,
            "total_checks": total,
            "missing": [k for k, v in checks.items() if not v],
        }


class ReviewerProfileCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating a reviewer profile."""

    # Declared explicitly so the schema renders an array; a bare JSONField is
    # mapped to a free-form object by JSONFieldExtension.
    alternative_names = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_("List of name variants used in publications"),
    )

    class Meta:
        model = models.ReviewerProfile
        fields = [
            "orcid_id",
            "biography",
            "alternative_names",
            "available_for_reviews",
        ]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


# =============================================================================
# COI (Conflict of Interest) Serializers
# =============================================================================


class CallCOIConfigurationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for per-call COI configuration."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    # Explicitly type JSON array fields for proper OpenAPI schema generation.
    recusal_required_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        required=False,
        help_text="COI types requiring automatic recusal",
    )
    management_allowed_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        required=False,
        help_text="COI types allowing management plan",
    )
    disclosure_only_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        required=False,
        help_text="COI types requiring disclosure only",
    )

    class Meta:
        model = models.CallCOIConfiguration
        fields = [
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "coauthorship_lookback_years",
            "coauthorship_threshold_papers",
            "institutional_lookback_years",
            "include_same_department",
            "include_same_institution",
            "recusal_required_types",
            "management_allowed_types",
            "disclosure_only_types",
            "auto_detect_coauthorship",
            "auto_detect_institutional",
            "auto_detect_named_personnel",
            "invitation_proposal_disclosure",
            "created",
            "modified",
        ]
        read_only_fields = ["call"]
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Combine incoming values with the persisted ones so partial (PATCH)
        # updates are validated against the effective post-save state.
        def effective(field):
            if field in attrs:
                return attrs[field] or []
            if self.instance is not None:
                return getattr(self.instance, field) or []
            return []

        overlaps = models.CallCOIConfiguration.find_rule_overlaps(
            {
                field: effective(field)
                for field in models.CallCOIConfiguration.RULE_FIELDS
            }
        )

        # Block every overlap this request would create, but leave configurations
        # that already overlapped alone: they predate the rule, and rewriting or
        # freezing them is out of scope. An overlap can only be introduced by
        # rewriting one of the rules holding it, so this still makes new ones
        # impossible while an untouched legacy one stays editable.
        # Keyed on the value actually changing, not on the field being present.
        def is_rewritten(field):
            if field not in attrs:
                return False
            if self.instance is None:
                return True
            return set(attrs[field] or []) != set(getattr(self.instance, field) or [])

        introduced = {
            coi_type: fields
            for coi_type, fields in overlaps.items()
            if any(is_rewritten(field) for field in fields)
        }
        if introduced:
            listed = "; ".join(
                "%s (%s)" % (coi_type, ", ".join(sorted(fields)))
                for coi_type, fields in sorted(introduced.items())
            )
            raise serializers.ValidationError(
                _(
                    "Each conflict type may only be assigned to one rule. "
                    "Remove it from all but one of these: %(conflicts)s."
                )
                % {"conflicts": listed}
            )
        return attrs


class ConflictOfInterestSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for conflict of interest records."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    round_name = serializers.ReadOnlyField(source="proposal.round.name")
    round_uuid = serializers.UUIDField(source="proposal.round.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    reviewed_by_name = serializers.ReadOnlyField(source="reviewed_by.full_name")
    conflicting_user_name = serializers.ReadOnlyField(
        source="conflicting_user.full_name"
    )
    conflicting_organization_name = serializers.ReadOnlyField(
        source="conflicting_organization.name"
    )
    coi_type_display = serializers.CharField(
        source="get_coi_type_display", read_only=True
    )
    severity_display = serializers.CharField(
        source="get_severity_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = models.ConflictOfInterest
        fields = [
            "url",
            "uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "round_uuid",
            "round_name",
            "call",
            "call_uuid",
            "call_name",
            "coi_type",
            "coi_type_display",
            "severity",
            "severity_display",
            "detection_method",
            "detected_at",
            "evidence_description",
            "evidence_data",
            "status",
            "status_display",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "management_plan",
            "conflicting_user",
            "conflicting_user_name",
            "conflicting_organization",
            "conflicting_organization_name",
            "created",
        ]
        read_only_fields = [
            "reviewer",
            "proposal",
            "call",
            "coi_type",
            "severity",
            "detection_method",
            "detected_at",
            "evidence_description",
            "evidence_data",
            "reviewed_by",
            "reviewed_at",
            "conflicting_user",
            "conflicting_organization",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewed_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "conflicting_user": {"lookup_field": "uuid", "view_name": "user-detail"},
            "conflicting_organization": {
                "lookup_field": "uuid",
                "view_name": "customer-detail",
            },
        }


class COIStatusUpdateSerializer(serializers.Serializer):
    """Serializer for updating COI status (dismiss/waive/recuse)."""

    status = serializers.ChoiceField(
        choices=[
            ("dismissed", "Dismiss - not a conflict"),
            ("waived", "Waive with management plan"),
            ("recused", "Recuse reviewer"),
        ]
    )
    review_notes = serializers.CharField(required=False, allow_blank=True)
    management_plan = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Required when status is 'waived'",
    )

    def validate(self, attrs):
        status = attrs.get("status")
        management_plan = attrs.get("management_plan")

        if status == "waived" and not management_plan:
            raise serializers.ValidationError(
                {
                    "management_plan": _(
                        "Management plan is required when waiving a conflict."
                    )
                }
            )

        return attrs


class ForceUnblockSerializer(serializers.Serializer):
    """Serializer for force-unblocking a COI-blocked assignment item."""

    override_reason = serializers.CharField(required=True)


class ForceAcceptPoolSerializer(serializers.Serializer):
    """Serializer for force-accepting a reviewer pool invitation."""

    override_reason = serializers.CharField(required=True)


class COIDisclosureFinancialInterestSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for financial interest disclosures."""

    class Meta:
        model = models.COIDisclosureFinancialInterest
        fields = [
            "uuid",
            "entity_name",
            "entity_type",
            "relationship_type",
            "amount_range",
            "is_ongoing",
            "description",
        ]


class COIDisclosureFormSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for COI disclosure forms."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    financial_interests = COIDisclosureFinancialInterestSerializer(
        many=True, read_only=True
    )

    class Meta:
        model = models.COIDisclosureForm
        fields = [
            "url",
            "uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "call",
            "call_uuid",
            "call_name",
            "certified",
            "certification_date",
            "certification_statement",
            "has_financial_interests",
            "financial_interests",
            "has_personal_relationships",
            "personal_relationships",
            "has_other_conflicts",
            "other_conflicts_description",
            "valid_until",
            "is_current",
            "created",
        ]
        read_only_fields = [
            "reviewer",
            "certification_date",
            "is_current",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "coi-disclosure-detail"},
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
                "required": False,
                "allow_null": True,
            },
        }


class COIPersonalRelationshipSerializer(serializers.Serializer):
    name = serializers.CharField()
    relationship_type = serializers.CharField()
    organization = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)


class COIDisclosureSubmitSerializer(serializers.Serializer):
    """Serializer for submitting a COI disclosure."""

    certification_statement = serializers.CharField()
    has_financial_interests = serializers.BooleanField(default=False)
    financial_interests = COIDisclosureFinancialInterestSerializer(
        many=True, required=False, default=list
    )
    has_personal_relationships = serializers.BooleanField(default=False)
    personal_relationships = COIPersonalRelationshipSerializer(
        many=True, required=False, default=list
    )
    has_other_conflicts = serializers.BooleanField(default=False)
    other_conflicts_description = serializers.CharField(
        required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        if attrs.get("has_financial_interests") and not attrs.get(
            "financial_interests"
        ):
            raise serializers.ValidationError(
                {
                    "financial_interests": _(
                        "Financial interests are required when indicated."
                    )
                }
            )
        if attrs.get("has_personal_relationships") and not attrs.get(
            "personal_relationships"
        ):
            raise serializers.ValidationError(
                {
                    "personal_relationships": _(
                        "Personal relationships must be specified."
                    )
                }
            )
        if attrs.get("has_other_conflicts") and not attrs.get(
            "other_conflicts_description"
        ):
            raise serializers.ValidationError(
                {
                    "other_conflicts_description": _(
                        "Other conflicts description is required."
                    )
                }
            )
        return attrs


class SelfDeclaredConflictSerializer(serializers.Serializer):
    """Serializer for reviewer self-declaring conflicts with specific proposals."""

    proposal_uuid = serializers.UUIDField()
    coi_type = serializers.ChoiceField(choices=COITypes.CHOICES)
    severity = serializers.ChoiceField(
        choices=COISeverityLevels.CHOICES,
        default=COISeverityLevels.APPARENT,
        required=False,
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_proposal_uuid(self, value):
        call = self.context.get("call")
        if not call:
            raise serializers.ValidationError(_("Call context is required."))
        try:
            proposal = models.Proposal.objects.get(uuid=value, round__call=call)
            return proposal
        except models.Proposal.DoesNotExist:
            raise serializers.ValidationError(_("Proposal not found in this call."))


# =============================================================================
# Reviewer Pool Serializers
# =============================================================================


class CallReviewerPoolSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for call reviewer pool membership.

    N+1 Optimization:
    When serializing multiple pool members, pass prefetched data via context:
    - coi_counts: dict mapping (reviewer_id, call_id) -> count
    - coi_by_severity: dict mapping (reviewer_id, call_id) -> {severity: count}
    - review_counts: dict mapping (user_id, call_id) -> {state: count}

    Or use annotations in the queryset:
    - annotated_coi_count
    - annotated_reviews_pending
    - annotated_reviews_in_progress
    - annotated_reviews_completed
    """

    reviewer_name = serializers.SerializerMethodField()
    reviewer_email = serializers.SerializerMethodField()
    reviewer_uuid = serializers.SerializerMethodField()
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    invited_by_name = serializers.ReadOnlyField(source="invited_by.full_name")
    invited_user_name = serializers.ReadOnlyField(source="invited_user.full_name")
    invitation_status_display = serializers.CharField(
        source="get_invitation_status_display", read_only=True
    )
    has_profile = serializers.SerializerMethodField()
    coi_count = serializers.SerializerMethodField()
    coi_by_severity = serializers.SerializerMethodField()
    reviews_pending = serializers.SerializerMethodField()
    reviews_in_progress = serializers.SerializerMethodField()
    reviews_completed = serializers.SerializerMethodField()
    overridden_by_name = serializers.ReadOnlyField(
        source="overridden_by.full_name", default=""
    )
    invitation_link = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_invitation_link(self, obj) -> str | None:
        """Return the frontend invitation link path for pending invitations.

        This is a frontend SPA route (not a backend API URL), so we cannot
        use Django's reverse(). The frontend route is defined in
        waldur-homeport/src/proposals/routes.ts as /reviewer-invitation/:token/.
        """
        if (
            obj.invitation_status != ReviewerPoolInvitationStatuses.PENDING
            or not obj.invitation_token
        ):
            return None
        return f"/reviewer-invitation/{obj.invitation_token}/"

    def get_reviewer_name(self, obj) -> str | None:
        """Get reviewer name from profile or invited_user."""
        if obj.reviewer:
            return obj.reviewer.user.full_name
        if obj.invited_user:
            return obj.invited_user.full_name
        return None

    def get_reviewer_email(self, obj) -> str | None:
        """Get email from profile, invited_user, or invited_email."""
        if obj.reviewer:
            return obj.reviewer.user.email
        if obj.invited_user:
            return obj.invited_user.email
        return obj.invited_email

    def get_reviewer_uuid(self, obj) -> str | None:
        """Get reviewer profile UUID if available."""
        if obj.reviewer:
            return str(obj.reviewer.uuid)
        return None

    def get_has_profile(self, obj) -> bool:
        """Check if reviewer has a profile."""
        return obj.reviewer is not None

    def get_coi_count(self, obj) -> int:
        """Count total COIs for this reviewer in this call."""
        if not obj.reviewer:
            return 0

        # Check for annotation first (most efficient)
        if hasattr(obj, "annotated_coi_count"):
            return obj.annotated_coi_count or 0

        # Check for prefetched data in context
        coi_counts = self.context.get("coi_counts")
        if coi_counts is not None:
            key = (obj.reviewer_id, obj.call_id)
            return coi_counts.get(key, 0)

        # Fallback to query (N+1 warning: avoid in list views)
        return models.ConflictOfInterest.objects.filter(
            reviewer=obj.reviewer,
            call=obj.call,
        ).count()

    def get_coi_by_severity(self, obj) -> dict:
        """Count COIs by severity level."""
        if not obj.reviewer:
            return {}

        # Check for prefetched data in context
        coi_by_severity = self.context.get("coi_by_severity")
        if coi_by_severity is not None:
            key = (obj.reviewer_id, obj.call_id)
            return coi_by_severity.get(key, {})

        # Fallback to query (N+1 warning: avoid in list views)
        from django.db.models import Count

        counts = (
            models.ConflictOfInterest.objects.filter(
                reviewer=obj.reviewer,
                call=obj.call,
            )
            .values("severity")
            .annotate(count=Count("id"))
        )
        return {item["severity"]: item["count"] for item in counts}

    def _get_reviewer_user(self, obj):
        """Get the user associated with this pool member."""
        if obj.reviewer:
            return obj.reviewer.user
        return obj.invited_user

    def _get_reviewer_user_id(self, obj):
        """Get user ID for review lookups."""
        if obj.reviewer:
            return obj.reviewer.user_id
        if obj.invited_user:
            return obj.invited_user_id
        return None

    def get_reviews_pending(self, obj) -> int:
        """Legacy field - always returns 0.

        Previously counted reviews in 'created' state, but that state
        has been removed. Reviews are now created directly in 'in_review' state.
        Kept for backwards compatibility with frontend.
        """
        return 0

    def get_reviews_in_progress(self, obj) -> int:
        """Count reviews in 'in_review' state."""
        # Check for annotation first
        if hasattr(obj, "annotated_reviews_in_progress"):
            return obj.annotated_reviews_in_progress or 0

        # Check for prefetched data in context
        review_counts = self.context.get("review_counts")
        if review_counts is not None:
            user_id = self._get_reviewer_user_id(obj)
            if user_id:
                key = (user_id, obj.call_id)
                counts = review_counts.get(key, {})
                return counts.get(models.Review.States.IN_REVIEW, 0)
            return 0

        # Fallback to query
        user = self._get_reviewer_user(obj)
        if not user:
            return 0
        return models.Review.objects.filter(
            reviewer=user,
            proposal__round__call=obj.call,
            state=models.Review.States.IN_REVIEW,
        ).count()

    def get_reviews_completed(self, obj) -> int:
        """Count reviews in 'submitted' state."""
        # Check for annotation first
        if hasattr(obj, "annotated_reviews_completed"):
            return obj.annotated_reviews_completed or 0

        # Check for prefetched data in context
        review_counts = self.context.get("review_counts")
        if review_counts is not None:
            user_id = self._get_reviewer_user_id(obj)
            if user_id:
                key = (user_id, obj.call_id)
                counts = review_counts.get(key, {})
                return counts.get(models.Review.States.SUBMITTED, 0)
            return 0

        # Fallback to query
        user = self._get_reviewer_user(obj)
        if not user:
            return 0
        return models.Review.objects.filter(
            reviewer=user,
            proposal__round__call=obj.call,
            state=models.Review.States.SUBMITTED,
        ).count()

    class Meta:
        model = models.CallReviewerPool
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "reviewer_email",
            "has_profile",
            "invited_email",
            "invited_user",
            "invited_user_name",
            "invited_at",
            "invitation_status",
            "invitation_status_display",
            "response_date",
            "decline_reason",
            "max_assignments",
            "current_assignments",
            "expertise_match_score",
            "invited_by_name",
            "invitation_link",
            "invitation_expires_at",
            "created",
            "coi_count",
            "coi_by_severity",
            "reviews_pending",
            "reviews_in_progress",
            "reviews_completed",
            "override_reason",
            "overridden_by_name",
            "overridden_at",
        ]
        read_only_fields = [
            "call",
            "reviewer",
            "invited_email",
            "invited_user",
            "invited_at",
            "invitation_status",
            "response_date",
            "decline_reason",
            "current_assignments",
            "invitation_expires_at",
            "override_reason",
            "overridden_at",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "invited_user": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class CallReviewerPoolUpdateSerializer(serializers.Serializer):
    """Serializer for updating reviewer pool member settings."""

    max_assignments = serializers.IntegerField(
        min_value=1,
        max_value=50,
        help_text="Maximum number of proposals that can be assigned to this reviewer",
    )

    def update(self, instance, validated_data):
        instance.max_assignments = validated_data.get(
            "max_assignments", instance.max_assignments
        )
        instance.save(update_fields=["max_assignments"])
        return instance


class ReviewerInvitationSerializer(serializers.Serializer):
    """Serializer for inviting reviewers to a call."""

    reviewer_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        help_text="List of reviewer profile UUIDs to invite",
    )
    max_assignments = serializers.IntegerField(default=5, min_value=1)
    invitation_message = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Custom message to include in invitation email",
    )


class ReviewerInvitationResponseSerializer(serializers.Serializer):
    """Serializer for responding to a reviewer invitation (via token)."""

    accept = serializers.BooleanField()
    decline_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Reason for declining (required if accept=False)",
    )

    def validate(self, attrs):
        if not attrs.get("accept") and not attrs.get("decline_reason"):
            raise serializers.ValidationError(
                {
                    "decline_reason": _(
                        "Reason is required when declining an invitation."
                    )
                }
            )
        return attrs


class EmailInvitationSerializer(serializers.Serializer):
    """Serializer for inviting a reviewer by email address."""

    email = serializers.EmailField(help_text="Email address to send the invitation to")
    invitation_message = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Custom message to include in invitation email",
    )
    max_assignments = serializers.IntegerField(default=5, min_value=1)


class ReviewerSuggestionTopMatchingProposalSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField(required=False)
    slug = serializers.CharField(required=False)
    affinity = serializers.FloatField()
    keyword_score = serializers.FloatField(required=False, allow_null=True)
    text_score = serializers.FloatField(required=False, allow_null=True)
    has_coi = serializers.BooleanField(required=False, allow_null=True)
    coi_type = serializers.CharField(required=False, allow_null=True)
    coi_severity = serializers.CharField(required=False, allow_null=True)


class ReviewerSuggestionSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for algorithm-generated reviewer suggestions."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_email = serializers.ReadOnlyField(source="reviewer.user.email")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    reviewer_biography = serializers.ReadOnlyField(source="reviewer.biography")
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    reviewed_by_name = serializers.ReadOnlyField(source="reviewed_by.full_name")

    source_type_display = serializers.CharField(
        source="get_source_type_display", read_only=True
    )
    matched_keywords = serializers.ListField(
        child=serializers.CharField(), read_only=True
    )
    top_matching_proposals = ReviewerSuggestionTopMatchingProposalSerializer(
        many=True, read_only=True
    )

    class Meta:
        model = models.ReviewerSuggestion
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "reviewer_email",
            "reviewer_biography",
            "affinity_score",
            "keyword_score",
            "text_score",
            "status",
            "status_display",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "rejection_reason",
            "matched_keywords",
            "top_matching_proposals",
            "source_type",
            "source_type_display",
            "created",
        ]
        read_only_fields = [
            "call",
            "reviewer",
            "affinity_score",
            "keyword_score",
            "text_score",
            "reviewed_by",
            "reviewed_at",
            "matched_keywords",
            "top_matching_proposals",
            "source_type",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "reviewed_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class SuggestionRejectSerializer(serializers.Serializer):
    """Serializer for rejecting a reviewer suggestion."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Reason for rejecting the suggestion",
    )


class OrcidCallbackSerializer(serializers.Serializer):
    """Serializer for ORCID OAuth callback."""

    code = serializers.CharField(
        help_text="Authorization code from ORCID OAuth callback",
    )
    state = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="State token for CSRF protection",
    )


class ImportPublicationsSerializer(serializers.Serializer):
    """Serializer for importing publications from various sources."""

    source = serializers.ChoiceField(
        choices=[("orcid", "ORCID"), ("doi", "DOI")],
        default="orcid",
        help_text="Source to import publications from",
    )
    doi = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="DOI of publication to import (required if source is 'doi')",
    )

    def validate(self, attrs):
        if attrs.get("source") == "doi" and not attrs.get("doi"):
            raise serializers.ValidationError(
                {"doi": _("DOI is required when source is 'doi'.")}
            )
        return attrs


# =============================================================================
# Matching Serializers
# =============================================================================


class MatchingConfigurationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for matching algorithm configuration."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.MatchingConfiguration
        fields = [
            "uuid",
            "call_uuid",
            "call_name",
            "affinity_method",
            "keyword_weight",
            "text_weight",
            "min_reviewers_per_proposal",
            "max_reviewers_per_proposal",
            "min_proposals_per_reviewer",
            "max_proposals_per_reviewer",
            "algorithm",
            "min_affinity_threshold",
            "use_reviewer_bids",
            "bid_weight",
            "created",
            "modified",
        ]


class ReviewerProposalAffinitySerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for reviewer-proposal affinity scores."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)

    class Meta:
        model = models.ReviewerProposalAffinity
        fields = [
            "uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "affinity_score",
            "keyword_score",
            "text_score",
            "created",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
        }


class AffinityMatrixEntrySerializer(serializers.Serializer):
    """Serializer for a single entry in the affinity matrix."""

    uuid = serializers.UUIDField()
    reviewer_uuid = serializers.UUIDField()
    reviewer_name = serializers.CharField()
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    affinity_score = serializers.FloatField()
    keyword_score = serializers.FloatField(allow_null=True)
    text_score = serializers.FloatField(allow_null=True)
    # COI fields
    has_conflict = serializers.BooleanField()
    coi_type = serializers.CharField(allow_null=True)
    coi_severity = serializers.CharField(allow_null=True)
    coi_status = serializers.CharField(allow_null=True)
    # Source field: "pool" or "suggestion"
    source = serializers.CharField()


class AffinityMatrixResponseSerializer(serializers.Serializer):
    """Serializer for the affinity matrix response."""

    count = serializers.IntegerField()
    results = AffinityMatrixEntrySerializer(many=True)


# Response serializers for action endpoints


class MessageResponseSerializer(serializers.Serializer):
    """Generic message response serializer."""

    message = serializers.CharField()


class DuplicateCallRequestSerializer(serializers.Serializer):
    """Request body for the protected-calls duplicate action."""

    name = serializers.CharField(
        max_length=models.Call._meta.get_field("name").max_length,
        required=True,
        allow_blank=False,
        trim_whitespace=True,
    )
    # Section flags mirror marketplace offering import (`include_*`). Each
    # defaults to True; uncheck to skip that part of the source's configuration.
    copy_documents = serializers.BooleanField(required=False, default=True)
    copy_offerings = serializers.BooleanField(required=False, default=True)
    copy_rounds = serializers.BooleanField(required=False, default=True)
    copy_workflow_steps = serializers.BooleanField(required=False, default=True)
    copy_resource_templates = serializers.BooleanField(required=False, default=True)
    copy_role_mappings = serializers.BooleanField(required=False, default=True)
    copy_applicant_visibility_config = serializers.BooleanField(
        required=False, default=True
    )
    copy_coi_configuration = serializers.BooleanField(required=False, default=True)
    copy_matching_configuration = serializers.BooleanField(required=False, default=True)
    copy_assignment_configuration = serializers.BooleanField(
        required=False, default=True
    )


class BulkRoundCreateRequestSerializer(serializers.ModelSerializer):
    """Request body for the rounds_bulk_set action.

    Combines a single round's configuration with cadence parameters so the
    server can spawn N evenly-spaced rounds in one shot. ``cutoff_time`` and
    ``allocation_date`` are intentionally excluded: cutoffs are derived from
    ``submission_window_days``; per-round fixed allocation dates don't make
    sense across a series and are explicitly disallowed in bulk mode.
    """

    cadence = serializers.ChoiceField(choices=BulkRoundCadence.CHOICES, required=True)
    custom_interval_months = serializers.IntegerField(
        min_value=1, required=False, allow_null=True
    )
    submission_window_days = serializers.IntegerField(min_value=1, required=True)
    number_of_rounds = serializers.IntegerField(
        min_value=1, max_value=60, required=True
    )

    class Meta:
        model = models.Round
        fields = [
            "start_time",
            "review_duration_in_days",
            "cadence",
            "custom_interval_months",
            "submission_window_days",
            "number_of_rounds",
        ]

    def validate(self, attrs):
        if attrs["cadence"] == BulkRoundCadence.CUSTOM and not attrs.get(
            "custom_interval_months"
        ):
            raise serializers.ValidationError(
                {"custom_interval_months": _("Required when cadence is set to custom.")}
            )
        return attrs


class ComputeAffinitiesResponseSerializer(serializers.Serializer):
    """Response for compute_affinities action."""

    computed_count = serializers.IntegerField()
    message = serializers.CharField()


class GenerateSuggestionsRequestSerializer(serializers.Serializer):
    """Request parameters for configurable suggestion generation."""

    source = serializers.ChoiceField(
        choices=[
            ("call_description", "Call Description"),
            ("all_proposals", "All Proposals"),
            ("selected_proposals", "Selected Proposals"),
            ("custom_keywords", "Custom Keywords"),
        ],
        default="all_proposals",
        help_text="What content to match reviewers against",
    )

    # For 'selected_proposals' source
    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        help_text="Specific proposal UUIDs to match against (for selected_proposals source)",
    )

    # For 'custom_keywords' source
    keywords = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
        help_text="Custom keywords to search for (for custom_keywords source)",
    )

    # Keyword search mode (for custom_keywords source)
    keyword_search_mode = serializers.ChoiceField(
        choices=[
            ("expertise_only", "Match against reviewer expertise keywords"),
            ("full_text", "Search all reviewer content"),
        ],
        default="expertise_only",
        required=False,
        help_text="How to search for custom keywords",
    )

    # Override default threshold
    min_affinity_threshold = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        help_text="Minimum affinity score for suggestions (0.0-1.0)",
    )

    def validate(self, attrs):
        source = attrs.get("source", "all_proposals")

        if source == "selected_proposals":
            if not attrs.get("proposal_uuids"):
                raise serializers.ValidationError(
                    {"proposal_uuids": "Required when source is 'selected_proposals'"}
                )

        if source == "custom_keywords":
            if not attrs.get("keywords"):
                raise serializers.ValidationError(
                    {"keywords": "Required when source is 'custom_keywords'"}
                )

        return attrs


class GenerateSuggestionsResponseSerializer(serializers.Serializer):
    """Response for generate_suggestions action."""

    suggestions_created = serializers.IntegerField()
    reviewers_evaluated = serializers.IntegerField()
    source_used = serializers.CharField()
    suggestions = serializers.ListField(child=serializers.CharField())


class SendInvitationsResponseSerializer(serializers.Serializer):
    """Response for send_invitations action."""

    invitations_sent = serializers.IntegerField()


class ConflictSummaryResponseSerializer(serializers.Serializer):
    """Response for conflict_summary action."""

    total = serializers.IntegerField()
    by_status = serializers.DictField(child=serializers.IntegerField())
    by_severity = serializers.DictField(child=serializers.IntegerField())
    by_type = serializers.DictField(child=serializers.IntegerField())


class OrcidSyncResponseSerializer(serializers.Serializer):
    """Response for sync_orcid action."""

    imported = serializers.DictField()
    last_sync = serializers.DateTimeField()


class OrcidDisconnectResponseSerializer(serializers.Serializer):
    """Response for disconnect_orcid action."""

    detail = serializers.CharField()


class ProposedAssignmentSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for proposed reviewer-proposal assignments."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    deployed_by_name = serializers.ReadOnlyField(source="deployed_by.full_name")

    class Meta:
        model = models.ProposedAssignment
        fields = [
            "url",
            "uuid",
            "call",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "affinity_score",
            "algorithm_used",
            "rank",
            "is_deployed",
            "deployed_at",
            "deployed_by",
            "deployed_by_name",
            "created",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "deployed_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class COIDetectionJobSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for COI detection job status."""

    url = serializers.SerializerMethodField()
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    progress_percentage = serializers.SerializerMethodField()

    class Meta:
        model = models.COIDetectionJob
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "job_type",
            "state",
            "total_pairs",
            "processed_pairs",
            "progress_percentage",
            "conflicts_found",
            "started_at",
            "completed_at",
            "error_message",
            "created",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }

    def get_url(self, obj) -> str:
        """Return URL for the job detail endpoint."""
        from rest_framework.reverse import reverse

        request = self.context.get("request")
        return reverse(
            "coi-detection-job-detail",
            kwargs={"uuid": obj.uuid.hex},
            request=request,
        )

    def get_progress_percentage(self, obj) -> float:
        if obj.total_pairs == 0:
            return 0.0
        return round(obj.processed_pairs / obj.total_pairs * 100, 1)


class TriggerCOIDetectionSerializer(serializers.Serializer):
    """Serializer for triggering COI detection."""

    job_type = serializers.ChoiceField(
        choices=[
            ("full_call", "Full call detection"),
            ("incremental", "Incremental detection"),
        ],
        default="full_call",
    )


class DeployAssignmentsSerializer(serializers.Serializer):
    """Serializer for deploying proposed assignments as actual reviews."""

    assignment_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Specific assignments to deploy. If empty, deploys all non-deployed assignments.",
    )


# =============================================================================
# Reviewer Bid Serializers
# =============================================================================


class ReviewerBidSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for reviewer bids."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    bid_display = serializers.CharField(source="get_bid_display", read_only=True)

    class Meta:
        model = models.ReviewerBid
        fields = [
            "uuid",
            "call",
            "call_uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "bid",
            "bid_display",
            "comment",
            "submitted_at",
            "modified_at",
        ]
        read_only_fields = [
            "call",
            "reviewer",
            "submitted_at",
            "modified_at",
        ]
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
        }


class ReviewerBidSubmitSerializer(serializers.Serializer):
    """Serializer for submitting a bid on a proposal."""

    proposal_uuid = serializers.UUIDField()
    bid = serializers.ChoiceField(
        choices=[
            ("eager", "Eager to review"),
            ("willing", "Willing to review"),
            ("not_willing", "Not willing to review"),
            ("conflict", "Has conflict of interest"),
        ]
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class ReviewerBulkBidSerializer(serializers.Serializer):
    """Serializer for submitting multiple bids at once."""

    bids = ReviewerBidSubmitSerializer(many=True)


# =============================================================================
# Public Invitation Serializers
# =============================================================================


class InvitationCOIConfigurationSerializer(serializers.Serializer):
    """COI configuration info for invitation display."""

    recusal_required_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        help_text="COI types requiring automatic recusal",
    )
    management_allowed_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        help_text="COI types where a management plan can be submitted",
    )
    disclosure_only_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        help_text="COI types that only need disclosure",
    )
    proposal_disclosure_level = serializers.CharField(
        help_text="How much proposal info is disclosed to reviewers",
    )


class InvitationProposalSummarySerializer(serializers.Serializer):
    """Proposal summary for invitation display."""

    uuid = serializers.UUIDField()
    name = serializers.CharField()
    summary = serializers.CharField(required=False, allow_null=True)


class COITypeChoiceSerializer(serializers.Serializer):
    """COI type choice tuple (value, label)."""

    value = serializers.CharField()
    label = serializers.CharField()


class PublicInvitationSerializer(serializers.Serializer):
    """Serializer for public invitation information (no auth required)."""

    call_name = serializers.CharField(read_only=True)
    call_uuid = serializers.UUIDField(read_only=True)
    invitation_status = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    is_expired = serializers.BooleanField(read_only=True)
    max_assignments = serializers.IntegerField(read_only=True, allow_null=True)
    invited_by_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Name of the person who sent the invitation",
    )
    profile_status = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="User's profile status: 'published', 'unpublished', 'missing', or null if not authenticated",
    )
    requires_profile = serializers.BooleanField(
        read_only=True,
        help_text="Whether the invitation requires creating a reviewer profile",
    )
    coi_configuration = InvitationCOIConfigurationSerializer(
        read_only=True,
        allow_null=True,
        help_text="COI configuration for this call",
    )
    coi_types = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField()),
        read_only=True,
        help_text="Available COI types as list of [value, label] tuples",
    )
    proposals = InvitationProposalSummarySerializer(
        many=True,
        read_only=True,
        help_text="Proposals for which conflicts can be declared",
    )


class InvitationAcceptSerializer(serializers.Serializer):
    """Serializer for accepting a reviewer invitation.

    Optionally includes self-declared conflicts with proposals.
    """

    declared_conflicts = SelfDeclaredConflictSerializer(
        many=True,
        required=False,
        help_text="Optional list of self-declared conflicts with proposals. "
        "Each conflict creates a ConflictOfInterest record with detection_method='self_disclosed'.",
    )


class InvitationDeclineSerializer(serializers.Serializer):
    """Serializer for declining a reviewer invitation."""

    reason = serializers.CharField(
        required=True,
        help_text="Reason for declining the invitation",
    )


# Response serializers for invitation actions
class InvitationAcceptResponseSerializer(serializers.Serializer):
    """Response for successful invitation acceptance."""

    detail = serializers.CharField()
    declared_conflicts = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="UUIDs of created conflict records",
    )


class InvitationAcceptErrorSerializer(serializers.Serializer):
    """Response for invitation acceptance errors (400)."""

    error = serializers.CharField()
    message = serializers.CharField()
    profile_url = serializers.CharField(required=False)


class InvitationAuthErrorSerializer(serializers.Serializer):
    """Response for unauthenticated invitation acceptance (401)."""

    error = serializers.CharField()


class InvitationDeclineResponseSerializer(serializers.Serializer):
    """Response for successful invitation decline."""

    detail = serializers.CharField()


# =============================================================================
# Assignment Batch Serializers (Stage 2 - Proposal Assignment Workflow)
# =============================================================================


class CallAssignmentConfigurationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for per-call assignment configuration."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.CallAssignmentConfiguration
        fields = [
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "auto_reassign_on_decline",
            "max_auto_reassign_attempts",
            "assignment_expiration_days",
            "send_reminder_before_expiry_days",
            "created",
            "modified",
        ]
        read_only_fields = ["call"]
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }


class AssignmentItemSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for individual assignment items within a batch."""

    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    review_uuid = serializers.UUIDField(source="review.uuid", read_only=True)
    coi_count = serializers.SerializerMethodField()
    overridden_by_name = serializers.ReadOnlyField(
        source="overridden_by.full_name", default=""
    )

    class Meta:
        model = models.AssignmentItem
        fields = [
            "url",
            "uuid",
            "batch",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "proposal_slug",
            "status",
            "status_display",
            "affinity_score",
            "has_coi",
            "coi_count",
            "responded_at",
            "decline_reason",
            "review",
            "review_uuid",
            "reassign_count",
            "override_reason",
            "overridden_by_name",
            "overridden_at",
            "created",
        ]
        read_only_fields = [
            "batch",
            "proposal",
            "status",
            "affinity_score",
            "has_coi",
            "responded_at",
            "review",
            "reassign_count",
            "override_reason",
            "overridden_at",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "batch": {
                "lookup_field": "uuid",
                "view_name": "assignment-batch-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "review": {
                "lookup_field": "uuid",
                "view_name": "proposal-review-detail",
            },
        }

    def get_coi_count(self, obj) -> int:
        """Count of COI records blocking this assignment."""
        return obj.coi_records.count()


class ExtendDeadlineRequestSerializer(serializers.Serializer):
    """Request serializer for extending assignment batch deadline."""

    expires_at = serializers.DateTimeField(
        help_text=_("New expiration date and time for the assignment batch.")
    )

    def validate_expires_at(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError(_("New deadline must be in the future."))
        return value


class ExtendDeadlineResponseSerializer(serializers.Serializer):
    """Response serializer confirming the new deadline."""

    expires_at = serializers.DateTimeField(help_text=_("The updated expiration date."))
    status = serializers.CharField(help_text=_("Current status of the batch."))


class AssignmentBatchSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for assignment batches sent to reviewers."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    reviewer_name = serializers.SerializerMethodField()
    reviewer_email = serializers.SerializerMethodField()
    reviewer_uuid = serializers.SerializerMethodField()
    reviewer_pool_entry_uuid = serializers.UUIDField(
        source="reviewer_pool_entry.uuid", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    items = AssignmentItemSerializer(many=True, read_only=True)
    items_count = serializers.SerializerMethodField()
    items_pending_count = serializers.SerializerMethodField()
    items_accepted_count = serializers.SerializerMethodField()
    items_declined_count = serializers.SerializerMethodField()
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.AssignmentBatch
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "reviewer_pool_entry",
            "reviewer_pool_entry_uuid",
            "reviewer_name",
            "reviewer_email",
            "reviewer_uuid",
            "status",
            "status_display",
            "sent_at",
            "expires_at",
            "responded_at",
            "source",
            "source_display",
            "created_by",
            "created_by_name",
            "manager_notes",
            "items",
            "items_count",
            "items_pending_count",
            "items_accepted_count",
            "items_declined_count",
            "is_expired",
            "created",
        ]
        read_only_fields = [
            "call",
            "reviewer_pool_entry",
            "status",
            "sent_at",
            "expires_at",
            "responded_at",
            "source",
            "created_by",
            "items",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer_pool_entry": {
                "lookup_field": "uuid",
                "view_name": "call-reviewer-pool-detail",
            },
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_reviewer_name(self, obj) -> str | None:
        """Get reviewer name from pool entry."""
        if obj.reviewer_pool_entry.reviewer:
            return obj.reviewer_pool_entry.reviewer.user.full_name
        if obj.reviewer_pool_entry.invited_user:
            return obj.reviewer_pool_entry.invited_user.full_name
        return None

    def get_reviewer_email(self, obj) -> str | None:
        """Get reviewer email from pool entry."""
        if obj.reviewer_pool_entry.reviewer:
            return obj.reviewer_pool_entry.reviewer.user.email
        if obj.reviewer_pool_entry.invited_user:
            return obj.reviewer_pool_entry.invited_user.email
        return obj.reviewer_pool_entry.invited_email

    def get_reviewer_uuid(self, obj) -> str | None:
        """Get reviewer profile UUID if available."""
        if obj.reviewer_pool_entry.reviewer:
            return str(obj.reviewer_pool_entry.reviewer.uuid)
        return None

    def get_items_count(self, obj) -> int:
        """Total count of items in batch."""
        return obj.items.count()

    def get_items_pending_count(self, obj) -> int:
        """Count of pending items."""
        return obj.items_pending_count

    def get_items_accepted_count(self, obj) -> int:
        """Count of accepted items."""
        return obj.items_accepted_count

    def get_items_declined_count(self, obj) -> int:
        """Count of declined items."""
        return obj.items_declined_count


class AssignmentBatchListSerializer(AssignmentBatchSerializer):
    """Lightweight serializer for listing assignment batches (without nested items)."""

    class Meta(AssignmentBatchSerializer.Meta):
        fields = [f for f in AssignmentBatchSerializer.Meta.fields if f != "items"]


# Action serializers for assignment endpoints


class GenerateAssignmentsSerializer(serializers.Serializer):
    """Serializer for generating assignment batches from the algorithm."""

    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Specific proposal UUIDs to generate assignments for. "
        "If empty, generates for all submitted proposals needing reviewers.",
    )
    reviewers_per_proposal = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=20,
        help_text="Number of reviewers to assign per proposal. "
        "If not specified, uses call's minimum_number_of_reviewers setting.",
    )


class SkippedProposalSerializer(serializers.Serializer):
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    reason = serializers.CharField()


class GenerateAssignmentsResponseSerializer(serializers.Serializer):
    """Response for generate_assignments action."""

    batches_created = serializers.IntegerField()
    items_created = serializers.IntegerField()
    proposals_processed = serializers.IntegerField()
    skipped_proposals = SkippedProposalSerializer(
        many=True,
        help_text="Proposals that were skipped with reasons",
    )


class SendAssignmentBatchSerializer(serializers.Serializer):
    """Serializer for sending an assignment batch invitation."""

    manager_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes to include in the invitation email",
    )


class SendAssignmentBatchResponseSerializer(serializers.Serializer):
    """Response for send assignment batch action."""

    detail = serializers.CharField()
    expires_at = serializers.DateTimeField()


class SendAllAssignmentBatchesSerializer(serializers.Serializer):
    """Serializer for sending all draft assignment batches."""

    batch_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Specific batch UUIDs to send. If empty, sends all draft batches.",
    )


class SendAllAssignmentBatchesResponseSerializer(serializers.Serializer):
    """Response for send_all_batches action."""

    batches_sent = serializers.IntegerField()
    skipped = serializers.IntegerField()


class AssignmentItemAcceptSerializer(serializers.Serializer):
    """Serializer for accepting an assignment item."""

    # No fields needed - just confirms acceptance
    pass


class AssignmentItemDeclineSerializer(serializers.Serializer):
    """Serializer for declining an assignment item."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Reason for declining this assignment",
    )


class AssignmentItemResponseSerializer(serializers.Serializer):
    """Response for assignment item accept/decline actions."""

    detail = serializers.CharField()
    review_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="UUID of created review (only on accept)",
    )


class ReassignItemSerializer(serializers.Serializer):
    """Serializer for reassigning a declined item to another reviewer."""

    reviewer_pool_entry_uuid = serializers.UUIDField(
        help_text="UUID of the pool entry for the new reviewer",
    )
    manager_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Notes to include in the reassignment notification",
    )


class ReassignItemResponseSerializer(serializers.Serializer):
    """Response for reassign action."""

    detail = serializers.CharField()
    new_item_uuid = serializers.UUIDField()
    new_batch_uuid = serializers.UUIDField()


class ReviewerSuggestionItemSerializer(serializers.Serializer):
    pool_entry_uuid = serializers.UUIDField()
    reviewer_name = serializers.CharField()
    affinity_score = serializers.FloatField(allow_null=True)
    current_assignments = serializers.IntegerField()
    max_assignments = serializers.IntegerField()


class SuggestAlternativeReviewersSerializer(serializers.Serializer):
    """Response for suggesting alternative reviewers for a declined item."""

    suggestions = ReviewerSuggestionItemSerializer(
        many=True,
        help_text="List of alternative reviewers with affinity scores",
    )


# Serializer for reviewer's view of their own assignments
class MyAssignmentBatchSerializer(serializers.Serializer):
    """Serializer for reviewer's view of their pending assignment batches."""

    uuid = serializers.UUIDField()
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    sent_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(allow_null=True)
    is_expired = serializers.BooleanField()
    items_count = serializers.IntegerField()
    items_pending_count = serializers.IntegerField()
    manager_notes = serializers.CharField(allow_blank=True)


class MyAssignmentItemSerializer(serializers.Serializer):
    """Serializer for individual assignment items in reviewer's view."""

    uuid = serializers.UUIDField()
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    proposal_slug = serializers.CharField()
    proposal_summary = serializers.CharField(allow_blank=True)
    status = serializers.CharField()
    status_display = serializers.CharField()
    affinity_score = serializers.FloatField(allow_null=True)
    has_coi = serializers.BooleanField()


class MyAssignmentBatchDetailSerializer(MyAssignmentBatchSerializer):
    """Detailed serializer for reviewer's view of a specific batch."""

    items = MyAssignmentItemSerializer(many=True)


class CreateManualAssignmentSerializer(serializers.Serializer):
    """Serializer for creating a manual assignment batch."""

    reviewer_pool_entry_uuid = serializers.UUIDField(
        help_text="UUID of the reviewer pool entry to assign proposals to",
    )
    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="List of proposal UUIDs to assign to the reviewer",
    )
    manager_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes about this assignment",
    )


class CreateManualAssignmentResponseSerializer(serializers.Serializer):
    """Response for create_manual_assignment action."""

    batch_uuid = serializers.UUIDField()
    items_created = serializers.IntegerField()
    skipped_proposals = SkippedProposalSerializer(
        many=True,
        help_text="Proposals that were skipped with reasons",
    )


# =============================================================================
# Workflow Step Serializers
# =============================================================================


class WorkflowCriterionSerializer(serializers.ModelSerializer):
    uuid = serializers.UUIDField(read_only=True)

    class Meta:
        model = models.WorkflowCriterion
        fields = ["uuid", "name", "order"]


CRITERIA_ALLOWED_STEPS = {"expert_review"}
AWARD_RESPONSE_ALLOWED_STEPS = {"allocation_decision"}
ALLOCATION_TIMING_ALLOWED_STEPS = {"allocation_decision"}


class CallWorkflowStepSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    call_uuid = serializers.ReadOnlyField(source="call.uuid")
    call_name = serializers.ReadOnlyField(source="call.name")

    checklist = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=checklist_models.Checklist.objects.all(),
        required=False,
        allow_null=True,
    )
    checklist_name = serializers.SerializerMethodField()
    is_mandatory = serializers.SerializerMethodField()
    criteria = WorkflowCriterionSerializer(many=True, required=False)

    class Meta:
        model = models.CallWorkflowStep
        fields = [
            "uuid",
            "created",
            "modified",
            "step",
            "call_uuid",
            "call_name",
            "is_enabled",
            "is_mandatory",
            "duration_in_days",
            "checklist",
            "checklist_name",
            "checklist_required",
            "blind_review",
            "requires_coi_confirmation",
            "min_reviewers",
            "min_score_threshold",
            "applicant_visible",
            "responsible_role",
            "transition_mode",
            "include_award_response",
            "allocation_time",
            "display_order",
            "criteria",
        ]
        read_only_fields = ("uuid", "created", "modified")
        protected_fields = ("call", "step")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_checklist_name(self, obj):
        return obj.checklist.name if obj.checklist else None

    @extend_schema_field(serializers.BooleanField())
    def get_is_mandatory(self, obj):
        # Mandatory steps can't be disabled and are required for call
        # activation; the UI mirrors this to gate the Activate button.
        return obj.step in MANDATORY_STEPS

    def validate(self, attrs):
        attrs = super().validate(attrs)
        step = attrs.get("step") or (self.instance.step if self.instance else None)

        if self.instance is None and step == "award_response":
            raise serializers.ValidationError(
                {
                    "step": (
                        "award_response is provisioned automatically via the "
                        "allocation_decision step's include_award_response flag "
                        "and cannot be added directly."
                    )
                }
            )

        if (
            attrs.get("include_award_response")
            and step not in AWARD_RESPONSE_ALLOWED_STEPS
        ):
            raise serializers.ValidationError(
                {
                    "include_award_response": (
                        "include_award_response can only be set on the "
                        "allocation_decision step."
                    )
                }
            )

        if (
            attrs.get("allocation_time") == AllocationTimes.FIXED_DATE
            and step not in ALLOCATION_TIMING_ALLOWED_STEPS
        ):
            raise serializers.ValidationError(
                {
                    "allocation_time": (
                        "allocation_time can only be configured on the "
                        "allocation_decision step."
                    )
                }
            )

        criteria = attrs.get("criteria")
        if criteria and step not in CRITERIA_ALLOWED_STEPS:
            raise serializers.ValidationError(
                {
                    "criteria": (
                        "Criteria can only be configured on the expert_review step."
                    )
                }
            )

        candidate = models.CallWorkflowStep()
        candidate.pk = getattr(self.instance, "pk", None)
        candidate.call = attrs.get("call", getattr(self.instance, "call", None))
        candidate.step = step
        candidate.is_enabled = attrs.get(
            "is_enabled", getattr(self.instance, "is_enabled", True)
        )
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)

        return attrs

    def create(self, validated_data):
        criteria = validated_data.pop("criteria", None)
        instance = super().create(validated_data)
        if criteria is not None:
            self._sync_criteria(instance, criteria)
        self._sync_award_response_step(instance)
        return instance

    def update(self, instance, validated_data):
        criteria = validated_data.pop("criteria", None)
        instance = super().update(instance, validated_data)
        if criteria is not None:
            self._sync_criteria(instance, criteria)
        self._sync_award_response_step(instance)
        return instance

    def _sync_criteria(self, instance, criteria):
        names = [c["name"] for c in criteria]
        instance.criteria.exclude(name__in=names).delete()
        for entry in criteria:
            models.WorkflowCriterion.objects.update_or_create(
                workflow_step=instance,
                name=entry["name"],
                defaults={"order": entry.get("order", 0)},
            )

    def _sync_award_response_step(self, instance):
        """Provision/disable the award_response step based on include_award_response."""
        if instance.step != "allocation_decision":
            return
        if instance.include_award_response:
            models.CallWorkflowStep.objects.update_or_create(
                call=instance.call,
                step="award_response",
                defaults={"is_enabled": True},
            )
        else:
            models.CallWorkflowStep.objects.filter(
                call=instance.call, step="award_response"
            ).update(is_enabled=False)


class StepChecklistStatusSerializer(serializers.Serializer):
    """Compact per-step checklist status surfaced on workflow_states so the UI
    can show a badge and gate the Complete button."""

    has_checklist = serializers.BooleanField()
    checklist_required = serializers.BooleanField()
    checklist_name = serializers.CharField(allow_null=True)
    checklist_completed = serializers.BooleanField()
    unanswered_required_count = serializers.IntegerField()


class ProposalWorkflowStepInstanceSerializer(serializers.ModelSerializer):
    step_name = serializers.SerializerMethodField()
    step_description = serializers.SerializerMethodField()
    responsible_role = serializers.SerializerMethodField()
    completed_by = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    applicant_visible = serializers.SerializerMethodField()
    duration_in_days = serializers.SerializerMethodField()
    is_required = serializers.SerializerMethodField()
    rejection_reason = serializers.SerializerMethodField()
    # Declared as a SerializerMethodField (rather than letting ModelSerializer
    # auto-generate it from the model field) so the schema can mark it as
    # nullable: the response is asymmetric — call-management team gets the
    # text, everyone else gets null. Without allow_null=True the generated
    # SDK types it as a required string and frontends crash on the applicant
    # view (CLAUDE.md "Nullable FKs MUST use allow_null=True").
    internal_notes = serializers.SerializerMethodField()
    checklist_status = serializers.SerializerMethodField()

    class Meta:
        model = models.ProposalWorkflowStepInstance
        fields = [
            "uuid",
            "step",
            "step_name",
            "step_description",
            "responsible_role",
            "status",
            "outcome",
            "outcome_reason",
            "rejection_reason",
            "internal_notes",
            "started_at",
            "completed_at",
            "completed_by",
            "deadline",
            "applicant_visible",
            "duration_in_days",
            "is_required",
            "checklist_status",
        ]
        read_only_fields = fields

    # Steps whose outcome/commentary is peer-review content, gated by the
    # call's reviews_visible_to_submitters setting (separate from the reviewer
    # *identity* gate that governs completed_by).
    REVIEW_STEPS = frozenset({"expert_review", "panel_review"})

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # ``completed_by`` reveals the identity of whoever completed the step —
        # including the reviewer on expert_review and the panel member on
        # panel_review. Honour the call's blind-review setting: null it for
        # proposal submitters (and any other non-call-team viewer) unless the
        # call reveals reviewer identity to submitters. ``can_view_step_actors``
        # is set by the view; defaulting to hidden fails closed if a future
        # caller forgets to set it. Mirrors the Review serializer's
        # reviewer_identity_visible_to_submitters gate.
        if not self.context.get("can_view_step_actors"):
            data["completed_by"] = None
        # Hide the reviewer/panel verdict and free-text commentary on the peer
        # review steps from submitters when the call keeps reviews private. This
        # must strip *every* field that surfaces the reviewer's words:
        # ``outcome`` (the verdict), ``outcome_reason`` (the raw commentary) and
        # ``rejection_reason`` — which is just ``outcome_reason`` re-exposed via a
        # SerializerMethodField and would otherwise leak the exact rejection text
        # the mask exists to hide. The applicant still learns the *decision* from
        # the proposal's own state and the call manager's allocation comment;
        # what stays private here is the reviewer's step-level reasoning.
        if instance.step in self.REVIEW_STEPS and not self.context.get(
            "can_view_review_content"
        ):
            data["outcome"] = None
            data["outcome_reason"] = ""
            data["rejection_reason"] = None
        return data

    def _get_call_step(self, obj):
        """Resolve the per-call ``CallWorkflowStep`` for this instance.

        When the view pre-loads configs into ``context['step_configs_by_key']``
        (recommended for list endpoints, max 6 rows per proposal), use that
        cache. Otherwise fall back to a per-instance query memoised on the
        serializer so the four SerializerMethodFields that consult it don't
        each issue their own SELECT — and re-issue it on every render of the
        same instance.
        """
        configs = self.context.get("step_configs_by_key")
        if configs is not None:
            return configs.get(obj.step)
        # Local cache keyed by (proposal_id, step). One query per (instance,
        # step) pair rather than per SerializerMethodField call.
        cache = self.context.setdefault("_call_step_fallback_cache", {})
        key = (obj.proposal_id, obj.step)
        if key in cache:
            return cache[key]
        config = (
            models.CallWorkflowStep.objects.filter(
                call_id=obj.proposal.round.call_id, step=obj.step
            )
            .only(
                "step",
                "applicant_visible",
                "duration_in_days",
                "responsible_role",
                "checklist",
                "checklist_required",
            )
            .first()
        )
        cache[key] = config
        return config

    @extend_schema_field(serializers.CharField())
    def get_step_name(self, obj):
        step_def = WORKFLOW_STEPS_MAP.get(obj.step)
        return step_def.name if step_def else obj.step

    @extend_schema_field(serializers.CharField())
    def get_step_description(self, obj):
        step_def = WORKFLOW_STEPS_MAP.get(obj.step)
        return step_def.description if step_def else ""

    @extend_schema_field(StepChecklistStatusSerializer(allow_null=True))
    def get_checklist_status(self, obj):
        call_step = self._get_call_step(obj)
        if call_step is None or not call_step.checklist_id:
            return None
        completion = obj.proposal.get_checklist_completion_for(call_step.checklist)
        if completion is not None:
            unanswered = completion.get_unanswered_required_questions().count()
        else:
            unanswered = call_step.checklist.questions.filter(required=True).count()
        return {
            "has_checklist": True,
            "checklist_required": call_step.checklist_required,
            "checklist_name": call_step.checklist.name,
            "checklist_completed": unanswered == 0,
            "unanswered_required_count": unanswered,
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_responsible_role(self, obj):
        # Per-call configuration takes precedence over the catalog default.
        config = self._get_call_step(obj)
        if config and config.responsible_role:
            return config.responsible_role
        step_def = WORKFLOW_STEPS_MAP.get(obj.step)
        return step_def.default_responsible_role if step_def else None

    @extend_schema_field(serializers.BooleanField())
    def get_applicant_visible(self, obj):
        # The applicant-visible flag is sourced from the per-call config; when
        # no config exists the conservative default is False so applicants
        # never see steps that were not explicitly opted into.
        config = self._get_call_step(obj)
        return bool(config and config.applicant_visible)

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_duration_in_days(self, obj):
        config = self._get_call_step(obj)
        return config.duration_in_days if config else None

    @extend_schema_field(serializers.BooleanField())
    def get_is_required(self, obj):
        # Sourced from the catalog, not per-call config: mandatory steps are
        # an invariant of the workflow definition, not configurable per call.
        step_def = WORKFLOW_STEPS_MAP.get(obj.step)
        return bool(step_def and step_def.is_mandatory)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_rejection_reason(self, obj):
        # Sugar: ``outcome_reason`` is the underlying field, but only carries
        # rejection text when ``outcome`` is ``rejected``. The frontend
        # otherwise has to know that convention; this surfaces it cleanly.
        #
        # Return the raw value (which is ``""`` when blank, not None) so a
        # rejected step with an empty reason is still distinguishable from a
        # non-rejected step. Frontends should branch on ``=== null`` to detect
        # "not rejected" rather than truthiness on the string.
        if obj.outcome == WorkflowStepOutcomes.REJECTED:
            return obj.outcome_reason
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_internal_notes(self, obj):
        # ``can_view_internal_notes`` is set by the view after the same
        # permission check used to gate write access. Returning None (not
        # absent) keeps the response shape stable so SDK consumers can type
        # the field as Optional[str] instead of "sometimes present".
        if not self.context.get("can_view_internal_notes"):
            return None
        return obj.internal_notes or None


class CompleteWorkflowStepSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=True,
        help_text=(
            "UUID of the workflow step instance the client believes is active. "
            "Used to detect concurrent step transitions."
        ),
    )
    outcome = serializers.ChoiceField(
        choices=WorkflowStepOutcomes.CHOICES,
        required=True,
        help_text=(
            "Step outcome. Must be in the active step's allow-list. "
            "'rejected' and 'expired' are reserved for system transitions."
        ),
    )
    outcome_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Explanation for the outcome.",
    )
    internal_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Internal notes captured by the call-management team. Stored on "
            "the step instance and never returned to applicants."
        ),
    )

    def validate_outcome(self, value):
        if value in WorkflowStepOutcomes.SYSTEM_RESERVED:
            raise serializers.ValidationError(
                f"'{value}' is system-reserved and cannot be supplied here."
            )
        active_step = self.context.get("active_step")
        if active_step:
            allowed = WorkflowStepOutcomes.STEP_ALLOW_LIST.get(active_step, frozenset())
            if value not in allowed:
                raise serializers.ValidationError(
                    f"'{value}' is not a valid outcome for step '{active_step}'. "
                    f"Allowed: {sorted(allowed)}."
                )
        return value


class RejectWorkflowStepSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=True,
        help_text=(
            "UUID of the workflow step instance the client believes is active. "
            "Used to detect concurrent step transitions."
        ),
    )
    reason = serializers.CharField(
        required=True,
        help_text="Reason for rejecting the proposal at this step.",
    )
    internal_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Internal notes captured by the call-management team alongside "
            "the rejection. Never returned to applicants."
        ),
    )


class CompleteWorkflowStepResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    proposal_state = serializers.CharField(
        required=False,
        help_text="New proposal state when the workflow terminates.",
    )
    next_step = serializers.CharField(
        required=False,
        help_text="Identifier of the step that just became active.",
    )


class RejectWorkflowStepResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    proposal_state = serializers.CharField()
