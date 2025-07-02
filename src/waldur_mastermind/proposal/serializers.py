import logging
from datetime import datetime

from constance import config
from django.db.models import Q
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.core.clean_html import clean_html
from waldur_core.permissions import enums as permissions_enums
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.serializers import (
    BasePublicPlanSerializer,
    OfferingComponentSerializer,
    OfferingOptionsField,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)

from . import models

logger = logging.getLogger(__name__)


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
            "provider_name",
            "category_uuid",
            "category_name",
            "call_managing_organisation",
            "attributes",
            "plan",
            "plan_details",
            "options",
            "components",
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


class ProposalReviewSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round_uuid = serializers.UUIDField(source="proposal.round.uuid", read_only=True)
    round_cutoff_time = serializers.ReadOnlyField(source="proposal.round.cutoff_time")
    round_start_time = serializers.ReadOnlyField(source="proposal.round.start_time")
    round_name = serializers.ReadOnlyField(source="proposal.round.name")
    call_uuid = serializers.UUIDField(source="proposal.round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="proposal.round.call.name")
    reviewer_full_name = serializers.ReadOnlyField(source="reviewer.full_name")
    reviewer_uuid = serializers.UUIDField(read_only=True, source="reviewer.uuid")
    anonymous_reviewer_name = serializers.SerializerMethodField()

    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(read_only=True, source="proposal.uuid")

    class Meta:
        model = models.Review
        fields = (
            "url",
            "uuid",
            "proposal",
            "proposal_name",
            "proposal_uuid",
            "reviewer",
            "reviewer_full_name",
            "reviewer_uuid",
            "anonymous_reviewer_name",
            "state",
            "review_end_date",
            "summary_score",
            "summary_public_comment",
            "summary_private_comment",
            "round_uuid",
            "round_name",
            "round_cutoff_time",
            "round_start_time",
            "call_name",
            "call_uuid",
            "comment_project_title",
            "comment_project_summary",
            "comment_project_is_confidential",
            "comment_project_has_civilian_purpose",
            "comment_project_description",
            "comment_project_duration",
            "comment_project_supporting_documentation",
            "comment_resource_requests",
            "comment_team",
        )
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
                models.Review.States.CREATED,
                models.Review.States.IN_REVIEW,
            ],
        ).exists()

        if existing_review:
            raise serializers.ValidationError(
                _("Review already exists for this proposal and reviewer.")
            )

        return super().create(validated_data)


class ReviewSubmitSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Review
        fields = (
            "summary_score",
            "summary_public_comment",
            "summary_private_comment",
        )


class ProtectedProposalListSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    approved_by_name = serializers.ReadOnlyField(source="approved_by.full_name")
    reviews = serializers.SerializerMethodField()

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
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
            "name",
            "start_time",
            "cutoff_time",
            "status",
            "review_strategy",
            "deciding_entity",
            "allocation_time",
            "allocation_date",
            "minimal_average_scoring",
            "review_duration_in_days",
            "minimum_number_of_reviewers",
        ]


class CallDocumentSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = serializers.IntegerField(source="file.size", read_only=True)

    class Meta:
        model = models.CallDocument
        fields = ["uuid", "file", "file_name", "file_size", "description", "created"]


class CallResourceTemplateSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    requested_offering_name = serializers.ReadOnlyField(
        source="requested_offering.offering.name"
    )
    requested_offering_uuid = serializers.UUIDField(
        source="requested_offering.uuid", read_only=True
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    url = serializers.SerializerMethodField()

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
            "created_by",
            "created_by_name",
            "created",
        ]
        read_only_fields = ("created_by",)
        extra_kwargs = {
            "requested_offering": {
                "lookup_field": "uuid",
                "view_name": "proposal-requested-offering-detail",
            },
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
    offerings = serializers.SerializerMethodField(method_name="get_offerings")
    rounds = serializers.SerializerMethodField()
    start_date = serializers.SerializerMethodField()
    end_date = serializers.SerializerMethodField()
    documents = CallDocumentSerializer(many=True, read_only=True)
    resource_templates = serializers.SerializerMethodField()
    fixed_duration_in_days = serializers.ReadOnlyField()

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

    def validate_description(self, value):
        return clean_html(value.strip())

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


class RequestedOfferingSerializer(
    core_serializers.AugmentedSerializerMixin, NestedRequestedOfferingSerializer
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


class ProtectedCallSerializer(PublicCallSerializer):
    reference_code = serializers.CharField(source="backend_id", required=False)
    default_project_role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True), slug_field="uuid", required=False
    )
    default_project_role_name = serializers.ReadOnlyField(
        source="default_project_role.name"
    )
    default_project_role_description = serializers.ReadOnlyField(
        source="default_project_role.description"
    )
    fixed_duration_in_days = serializers.IntegerField(required=False, allow_null=True)
    reviewer_identity_visible_to_submitters = serializers.BooleanField(
        help_text="Whether proposal submitters can see reviewer identities",
        required=False,
    )
    reviews_visible_to_submitters = serializers.BooleanField(
        help_text="Whether proposal submitters can see review comments and scores",
        required=False,
    )

    class Meta(PublicCallSerializer.Meta):
        fields = PublicCallSerializer.Meta.fields + (
            "created_by",
            "reference_code",
            "default_project_role",
            "default_project_role_name",
            "default_project_role_description",
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

    def validate_default_project_role(self, default_project_role: Role):
        if default_project_role.content_type.model_class() != Project:
            raise serializers.ValidationError("Role should belong to the project type.")
        return default_project_role

    def create(self, validated_data):
        request = self.context["request"]
        customer = validated_data.get("manager", None).customer
        if not permissions_utils.has_permission(
            request,
            permissions_enums.PermissionEnum.CREATE_CALL,
            customer,
        ):
            raise PermissionDenied()

        validated_data["created_by"] = request.user
        return super().create(validated_data)

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

        return super().update(instance, validated_data)


class ProtectedRoundSerializer(
    core_serializers.AugmentedSerializerMixin, NestedRoundSerializer
):
    url = serializers.SerializerMethodField()
    proposals = ProtectedProposalListSerializer(
        many=True, read_only=True, source="proposal_set"
    )
    review_duration_in_days = serializers.IntegerField(
        default=config.PROPOSAL_REVIEW_DURATION
    )

    class Meta(NestedRoundSerializer.Meta):
        fields = NestedRoundSerializer.Meta.fields + ["url", "proposals"]

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
        fields = ["file", "file_name", "file_size", "created"]


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


class ProposalSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round = NestedRoundSerializer(read_only=True)
    round_uuid = serializers.UUIDField(write_only=True, required=True)
    call_uuid = serializers.UUIDField(source="round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="round.call.name", read_only=True)
    supporting_documentation = ProposalDocumentationSerializer(
        many=True, read_only=True, source="proposaldocumentation_set"
    )
    oecd_fos_2007_label = serializers.ReadOnlyField(
        source="get_oecd_fos_2007_code_display"
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_uuid = serializers.UUIDField(source="created_by.uuid", read_only=True)
    project_name = serializers.ReadOnlyField(source="project.name")

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
            "url",
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
            "duration_in_days",
            "project",
            "round",
            "round_uuid",
            "call_uuid",
            "call_name",
            "oecd_fos_2007_code",
            "oecd_fos_2007_label",
            "allocation_comment",
            "created",
        ]
        read_only_fields = (
            "created_by",
            "approved_by",
            "project",
            "allocation_comment",
        )
        protected_fields = ("round_uuid",)
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "approved_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "project": {"lookup_field": "uuid", "view_name": "project-detail"},
        }

    def validate_description(self, value):
        return clean_html(value.strip())

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

        if call_round.status not in (
            models.Round.Statuses.SCHEDULED,
            models.Round.Statuses.OPEN,
        ):
            raise serializers.ValidationError(_("Round is not active."))

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

    def get_fields(self):
        fields = super().get_fields()

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


class RoundReviewerSerializer(serializers.Serializer):
    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    accepted_proposals = serializers.IntegerField()
    rejected_proposals = serializers.IntegerField()
    in_review_proposals = serializers.IntegerField()

    def get_full_name(self, obj) -> str:
        return f"{obj.first_name} {obj.last_name}"


class ProposalApproveSerializer(serializers.Serializer):
    allocation_comment = serializers.CharField(required=False)


class CallRoundSerializer(serializers.HyperlinkedModelSerializer):
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.Round
        fields = [
            "url",
            "uuid",
            "start_time",
            "cutoff_time",
            "call_uuid",
            "call_name",
            "status",
        ]

    extra_kwargs = {
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


class CallAttachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.FileField())
    description = serializers.CharField(required=False)


class CallDetachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.UUIDField())
