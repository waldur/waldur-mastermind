import logging

from constance import config
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.core.clean_html import clean_html
from waldur_core.media.serializers import (
    ProtectedImageField,
    ProtectedMediaSerializerMixin,
)
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.serializers import (
    BasePublicPlanSerializer,
    MarketplaceProtectedMediaSerializerMixin,
    OfferingComponentSerializer,
)

from . import models

logger = logging.getLogger(__name__)


class CallManagingOrganisationSerializer(
    MarketplaceProtectedMediaSerializerMixin,
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

    customer_image = ProtectedImageField(source="customer.image", read_only=True)
    customer_country = serializers.CharField(source="customer.country", read_only=True)

    def get_fields(self):
        fields = super().get_fields()
        if settings.WALDUR_MARKETPLACE["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"]:
            fields["customer_image"] = serializers.ImageField(
                source="customer.image", read_only=True
            )
        return fields

    def validate(self, attrs):
        if not self.instance:
            marketplace_permissions.can_register_service_provider(
                self.context["request"], attrs["customer"]
            )
        return attrs


class NestedRequestedOfferingSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_uuid = serializers.ReadOnlyField(source="offering.uuid")
    category_uuid = serializers.ReadOnlyField(source="offering.category.uuid")
    category_name = serializers.ReadOnlyField(source="offering.category.title")
    provider_name = serializers.ReadOnlyField(source="offering.customer.name")
    call_managing_organisation = serializers.ReadOnlyField(
        source="call.manager.customer.name"
    )
    options = serializers.JSONField(
        required=False,
        default={"options": {}, "order": []},
        read_only=True,
        source="offering.options",
    )
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

    def get_url(self, requested_offering):
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

    def get_url(self, requested_resource):
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-proposal-resource-detail",
                kwargs={
                    "uuid": requested_resource.proposal.uuid.hex,
                    "obj_uuid": requested_resource.uuid.hex,
                },
            )
        )

    class Meta:
        model = models.RequestedResource
        fields = [
            "uuid",
            "url",
            "requested_offering",
            "resource",
            "resource_name",
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


class ReviewSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round_uuid = serializers.UUIDField(source="proposal.round.uuid", read_only=True)
    round_cutoff_time = serializers.ReadOnlyField(source="proposal.round.cutoff_time")
    round_start_time = serializers.ReadOnlyField(source="proposal.round.start_time")
    call_uuid = serializers.UUIDField(source="proposal.round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="proposal.round.call.name")
    reviewer_full_name = serializers.ReadOnlyField(source="reviewer.full_name")

    proposal_name = serializers.ReadOnlyField(source="proposal.name")

    class Meta:
        model = models.Review
        fields = (
            "url",
            "uuid",
            "proposal",
            "reviewer",
            "reviewer_full_name",
            "state",
            "review_end_date",
            "summary_score",
            "summary_public_comment",
            "summary_private_comment",
            "proposal_name",
            "round_uuid",
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

    def get_fields(self):
        fields = super().get_fields()

        if not self.instance:
            return fields
        elif isinstance(self.instance, list):
            review = self.instance[0]
        else:
            review: models.Review = self.instance

        try:
            request = self.context["view"].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if (
            user.is_staff
            or review.reviewer == user
            or review.proposal.round.call.manager.customer.has_user(user)
        ):
            return fields

        del fields["summary_private_comment"]
        del fields["reviewer"]
        del fields["reviewer_full_name"]

        return fields


class ProtectedProposalListSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    approved_by_name = serializers.ReadOnlyField(source="approved_by.full_name")
    reviews = ReviewSerializer(many=True, read_only=True, source="review_set")

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


class NestedRoundSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Round
        fields = [
            "uuid",
            "name",
            "start_time",
            "cutoff_time",
            "review_strategy",
            "deciding_entity",
            "allocation_time",
            "allocation_date",
            "minimal_average_scoring",
            "review_duration_in_days",
            "minimum_number_of_reviewers",
        ]


class CallDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CallDocument
        fields = ["uuid", "file"]


class PublicCallSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    customer_name = serializers.ReadOnlyField(source="manager.customer.name")
    offerings = serializers.SerializerMethodField(method_name="get_offerings")
    rounds = NestedRoundSerializer(many=True, read_only=True, source="round_set")
    start_date = serializers.SerializerMethodField()
    end_date = serializers.SerializerMethodField()
    documents = CallDocumentSerializer(many=True, read_only=True)

    class Meta:
        model = models.Call
        fields = (
            "url",
            "uuid",
            "created",
            "start_date",
            "end_date",
            "name",
            "description",
            "state",
            "manager",
            "customer_name",
            "offerings",
            "rounds",
            "documents",
            "backend_id",
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
        }

    def get_start_date(self, obj):
        first_round = obj.round_set.order_by("start_time").first()
        return first_round.start_time if first_round else None

    def get_end_date(self, obj):
        last_round = obj.round_set.order_by("-cutoff_time").first()
        return last_round.cutoff_time if last_round else None

    def get_offerings(self, obj):
        queryset = obj.requestedoffering_set.filter(
            state=models.RequestedOffering.States.ACCEPTED
        )
        serializer = NestedRequestedOfferingSerializer(
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

    def get_url(self, requested_offering):
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
    requested_offering_uuid = serializers.UUIDField(write_only=True, required=True)

    class Meta(NestedRequestedResourceSerializer.Meta):
        fields = NestedRequestedResourceSerializer.Meta.fields + [
            "requested_offering_uuid"
        ]

        read_only_fields = (
            "created_by",
            "resource",
        )

    def validate(self, attrs):
        if self.instance:
            return attrs

        requested_offering_uuid = attrs.pop("requested_offering_uuid")
        proposal = attrs["proposal"]

        try:
            requested_offering = proposal.round.call.requestedoffering_set.get(
                uuid=requested_offering_uuid
            )
        except models.RequestedOffering.DoesNotExist:
            raise serializers.ValidationError(
                {"requested_offering_uuid": _("Requested offering has not been found.")}
            )

        if requested_offering.state != models.RequestedOffering.States.ACCEPTED:
            raise serializers.ValidationError(
                _("Offering has not been confirmed by service provider.")
            )

        attrs["requested_offering"] = requested_offering
        return attrs

    def validate_attributes(self, attributes):
        if not attributes:
            return {}

        return attributes

    def validate_proposal(self, proposal):
        if proposal.state != models.Proposal.States.DRAFT:
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
            },
        }

    def get_url(self, requested_resource):
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-requested-resource-detail",
                kwargs={
                    "uuid": requested_resource.uuid.hex,
                },
            )
        )


class ProviderRequestedOfferingSerializer(NestedRequestedOfferingSerializer):
    url = serializers.SerializerMethodField()
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
        }

    def get_url(self, requested_offering):
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-requested-offering-detail",
                kwargs={
                    "uuid": requested_offering.uuid.hex,
                },
            )
        )


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

        if manager and not user.is_staff and not manager.customer.has_user(user):
            raise serializers.ValidationError(
                "Current user does not belong to the selected organisation."
            )

        return manager

    def validate_default_project_role(self, default_project_role: Role):
        if default_project_role.content_type.model_class() != Project:
            raise serializers.ValidationError("Role should belong to the project type.")
        return default_project_role

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


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

    def get_url(self, call_round):
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


class ProposalDocumentationSerializer(ProtectedMediaSerializerMixin):
    class Meta:
        model = models.ProposalDocumentation
        fields = ["file"]


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

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
            "url",
            "name",
            "description",
            "project_summary",
            "project_is_confidential",
            "project_has_civilian_purpose",
            "supporting_documentation",
            "state",
            "approved_by",
            "created_by",
            "duration_in_days",
            "project",
            "round",
            "round_uuid",
            "call_uuid",
            "call_name",
            "oecd_fos_2007_code",
            "oecd_fos_2007_label",
            "allocation_comment",
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

        if call_round.call.state != models.Call.States.ACTIVE:
            raise serializers.ValidationError(_("Call is not active."))

        attrs["round"] = call_round
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class ReviewerSerializer(serializers.Serializer):
    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    accepted_proposals = serializers.IntegerField()
    rejected_proposals = serializers.IntegerField()
    in_review_proposals = serializers.IntegerField()

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"


class ProposalAllocateSerializer(serializers.Serializer):
    allocation_comment = serializers.CharField(required=False)


class RoundSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.Round
        fields = ["url", "uuid", "start_time", "cutoff_time", "call_uuid", "call_name"]

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

    def get_url(self, obj):
        return self.context["request"].build_absolute_uri(
            reverse(
                "call-round-detail",
                kwargs={
                    "uuid": obj.uuid.hex,
                },
            )
        )
