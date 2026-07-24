import logging
import os
from datetime import timedelta
from functools import cached_property

from constance import config
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.template import Context, Template
from django.template import exceptions as template_exceptions
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions, serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core.clean_html import clean_html
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.core.utils import is_uuid_like, text2html
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import models as structure_models
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.registry import get_resource_type
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.enums import (
    JIRA_WEBHOOK_EVENT_MAP,
    SupportWebhookEvent,
)

from . import backend, models

logger = logging.getLogger(__name__)


def render_issue_template(config_name, template_name, issue):
    try:
        template = get_template("support/" + template_name + ".txt").template
    except template_exceptions.TemplateDoesNotExist:
        raw = getattr(config, config_name)
        template = Template(raw)

    return template.render(
        Context(
            {"issue": issue, "settings": settings, "config": config}, autoescape=False
        )
    )


class NestedFeedbackSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.CharField(
        read_only=True,
        source="get_state_display",
        help_text="Current state of the feedback",
    )
    evaluation = serializers.IntegerField(
        read_only=True, help_text="Customer satisfaction rating (1-5 stars)"
    )
    evaluation_number = serializers.IntegerField(
        read_only=True, source="evaluation", help_text="Numeric value of the rating"
    )

    class Meta:
        model = models.Feedback
        fields = (
            "evaluation",
            "evaluation_number",
            "comment",
            "state",
        )


class IssueSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    resource = core_serializers.GenericRelatedField(
        related_models=structure_models.BaseResource.get_all_models()
        + [marketplace_models.Resource],
        required=False,
    )
    caller = serializers.HyperlinkedRelatedField(
        view_name="user-detail",
        lookup_field="uuid",
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )
    reporter = serializers.HyperlinkedRelatedField(
        view_name="support-user-detail", lookup_field="uuid", read_only=True
    )
    assignee = serializers.HyperlinkedRelatedField(
        view_name="support-user-detail",
        lookup_field="uuid",
        queryset=models.SupportUser.objects.all(),
        required=False,
        allow_null=True,
    )
    template = serializers.HyperlinkedRelatedField(
        view_name="support-template-detail",
        lookup_field="uuid",
        queryset=models.Template.objects.all(),
        required=False,
        allow_null=True,
    )
    parent_issue = serializers.HyperlinkedRelatedField(
        view_name="support-issue-detail",
        lookup_field="uuid",
        read_only=True,
    )
    provider_helpdesk = serializers.HyperlinkedRelatedField(
        view_name="provider-helpdesk-detail",
        lookup_field="uuid",
        read_only=True,
    )
    resource_type = serializers.SerializerMethodField()
    resource_name = serializers.CharField(read_only=True, source="resource.name")
    type = serializers.CharField()
    is_reported_manually = serializers.BooleanField(
        initial=False,
        default=False,
        write_only=True,
        help_text=_("Set true if issue is created by regular user via portal."),
    )
    feedback = NestedFeedbackSerializer(required=False, read_only=True, allow_null=True)
    update_is_available = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()
    add_comment_is_available = serializers.SerializerMethodField()
    add_attachment_is_available = serializers.SerializerMethodField()
    order_uuid = serializers.SerializerMethodField()
    order_project_uuid = serializers.SerializerMethodField()
    order_customer_uuid = serializers.SerializerMethodField()
    order_resource_name = serializers.SerializerMethodField()
    sla_status = serializers.SerializerMethodField()
    is_routed = serializers.SerializerMethodField()
    provider_ticket_info = serializers.SerializerMethodField()

    class Meta:
        model = models.Issue
        fields = (
            "url",
            "uuid",
            "type",
            "key",
            "backend_id",
            "backend_name",
            "remote_id",
            "link",
            "summary",
            "description",
            "status",
            "resolution",
            "priority",
            "caller",
            "caller_uuid",
            "caller_full_name",
            "reporter",
            "reporter_uuid",
            "reporter_name",
            "assignee",
            "assignee_uuid",
            "assignee_name",
            "customer",
            "customer_uuid",
            "customer_name",
            "project",
            "project_uuid",
            "project_name",
            "resource",
            "resource_type",
            "resource_name",
            "created",
            "modified",
            "is_reported_manually",
            "template",
            "feedback",
            "resolved",
            "update_is_available",
            "destroy_is_available",
            "add_comment_is_available",
            "add_attachment_is_available",
            "processing_log",
            "order_uuid",
            "order_project_uuid",
            "order_customer_uuid",
            "order_resource_name",
            "first_response_deadline",
            "resolution_deadline",
            "first_response_at",
            "sla_breached",
            "sla_status",
            "parent_issue",
            "provider_helpdesk",
            "is_escalated",
            "escalated_at",
            "escalation_reason",
            "is_routed",
            "provider_ticket_info",
        )
        read_only_fields = (
            "key",
            "status",
            "resolution",
            "backend_id",
            "backend_name",
            "link",
            "feedback",
            "processing_log",
            "first_response_deadline",
            "resolution_deadline",
            "first_response_at",
            "sla_breached",
            "sla_status",
            "parent_issue",
            "provider_helpdesk",
            "is_escalated",
            "escalated_at",
            "escalation_reason",
            "is_routed",
            "provider_ticket_info",
        )
        protected_fields = (
            "customer",
            "project",
            "resource",
            "type",
            "caller",
            "template",
            "priority",
            "remote_id",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid"},
            customer={"lookup_field": "uuid", "view_name": "customer-detail"},
            project={"lookup_field": "uuid", "view_name": "project-detail"},
        )
        related_paths = dict(
            caller=(
                "uuid",
                "full_name",
            ),
            reporter=(
                "uuid",
                "name",
            ),
            assignee=(
                "uuid",
                "name",
            ),
            customer=(
                "uuid",
                "name",
            ),
            project=(
                "uuid",
                "name",
            ),
        )

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        user = self.context["request"].user
        if user.is_authenticated and not user.is_staff and not user.is_support:
            del fields["link"]
            # Hide processing_log from non-staff users
            if "processing_log" in fields:
                del fields["processing_log"]

        if "type" in fields:
            # Get active request types from database
            active_types = list(
                models.RequestType.objects.filter(is_active=True).values_list(
                    "name", flat=True
                )
            )
            default_type = active_types[0] if active_types else ""

            fields["type"] = serializers.ChoiceField(
                choices=[(t, t) for t in active_types],
                initial=default_type,
                default=default_type,
            )

        return fields

    def validate_type(self, issue_type: str):
        # Validate against active RequestTypes from database
        active_types = models.RequestType.objects.filter(is_active=True)

        if not active_types.filter(name=issue_type).exists():
            allowed_names = ", ".join(active_types.values_list("name", flat=True))
            raise serializers.ValidationError(
                _("Issue type must be one of the following: %s.") % allowed_names
            )

        # If issue_type is empty, use the first active type
        if not issue_type:
            first_type = active_types.first()
            if first_type:
                issue_type = first_type.name

        return issue_type

    def get_resource_type(self, obj: models.Issue) -> str:
        resource = obj.safe_resource
        if (
            isinstance(resource, structure_models.BaseResource)
            and obj.resource_content_type
        ):
            return get_resource_type(obj.resource_content_type.model_class())
        if isinstance(resource, marketplace_models.Resource):
            return "Marketplace.Resource"
        return ""

    def get_update_is_available(self, obj: models.Issue) -> bool:
        return backend.get_active_backend().update_is_available(obj)

    def get_destroy_is_available(self, obj: models.Issue) -> bool:
        return backend.get_active_backend().destroy_is_available(obj)

    def get_add_comment_is_available(self, obj: models.Issue) -> bool:
        return backend.get_active_backend().comment_create_is_available(obj)

    def get_add_attachment_is_available(self, obj: models.Issue) -> bool:
        return backend.get_active_backend().attachment_create_is_available(obj)

    def get_is_routed(self, obj: models.Issue) -> bool:
        # Consume the prefetch cache (see IssueViewSet.get_queryset).
        return bool(obj.child_issues.all())

    def get_provider_ticket_info(self, obj: models.Issue) -> dict | None:
        # Consume the prefetch cache (see IssueViewSet.get_queryset).
        children = obj.child_issues.all()
        child = children[0] if children else None
        if child and child.provider_helpdesk:
            service_provider = child.provider_helpdesk.service_provider
            return {
                "child_issue_uuid": child.uuid.hex,
                "child_ticket_key": child.key,
                "child_ticket_status": child.status,
                "provider_name": str(service_provider),
                "provider_customer_uuid": service_provider.customer.uuid.hex,
                "backend_type": child.provider_helpdesk.backend_type,
            }
        return None

    def _can_view_routing(self, obj: models.Issue) -> bool:
        """Routing internals (provider identity, child ticket, parent link) are
        visible only to staff/support and to the provider's own support users.
        For a plain caller the provider relationship stays hidden."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or user.is_anonymous:
            return False
        if user.is_staff or user.is_support:
            return True
        helpdesk = obj.provider_helpdesk
        # `helpdesk` is None for non-routed / parent issues, so this short-circuits
        # without a query for the common caller case.
        return bool(
            helpdesk
            and helpdesk.support_users.filter(user=user, is_active=True).exists()
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self._can_view_routing(instance):
            for field in (
                "is_routed",
                "provider_ticket_info",
                "provider_helpdesk",
                "parent_issue",
            ):
                data.pop(field, None)
        return data

    def get_sla_status(self, obj: models.Issue) -> str:
        if obj.sla_breached:
            return "breached"
        if obj.resolution_date:
            return "met"
        from django.utils import timezone as tz

        now = tz.now()
        if obj.first_response_deadline and obj.first_response_at is None:
            if now > obj.first_response_deadline:
                return "breached"
        if obj.resolution_deadline and now > obj.resolution_deadline:
            return "breached"
        return "on_track"

    def get_order_uuid(self, obj: models.Issue) -> str | None:
        """Return order UUID if the issue's resource is an Order."""
        resource = obj.safe_resource
        if isinstance(resource, marketplace_models.Order):
            return resource.uuid.hex
        return None

    def get_order_project_uuid(self, obj: models.Issue) -> str | None:
        """Return order's project UUID if the issue's resource is an Order."""
        resource = obj.safe_resource
        if isinstance(resource, marketplace_models.Order):
            return resource.project.uuid.hex
        return None

    def get_order_customer_uuid(self, obj: models.Issue) -> str | None:
        """Return order's customer UUID if the issue's resource is an Order."""
        resource = obj.safe_resource
        if isinstance(resource, marketplace_models.Order):
            return resource.project.customer.uuid.hex
        return None

    def get_order_resource_name(self, obj: models.Issue) -> str | None:
        """Return order's resource name if the issue's resource is an Order."""
        order = obj.safe_resource
        if isinstance(order, marketplace_models.Order) and order.resource:
            return order.resource.name
        return None

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        request_user = self.context["request"].user
        if attrs.pop("is_reported_manually"):
            attrs["caller"] = request_user
            if attrs.get("assignee"):
                raise serializers.ValidationError(
                    {
                        "assignee": _(
                            "Assignee cannot be defined if issue is reported manually."
                        )
                    }
                )
        else:
            # create a request on behalf of an agent
            if not attrs.get("caller"):
                raise serializers.ValidationError(
                    {"caller": _("This field is required.")}
                )
            # if change of reporter is supported, use it
            if config.ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS:
                reporter = models.SupportUser.objects.filter(
                    user=request_user,
                    is_active=True,
                    backend_name=backend.get_active_backend().backend_name,
                ).first()
                if not reporter:
                    raise serializers.ValidationError(
                        _(
                            "You cannot report issues because your help desk account is not connected to profile."
                        )
                    )
                attrs["reporter"] = reporter
            else:
                # leave a mark about reporter in the description field
                attrs["description"] = (
                    f"Reported by {request_user.full_name}.\n\n"
                    + attrs.get("description", "")
                )

        return attrs

    def validate_summary(self, summary):
        """
        Remove leading and trailing spaces from summary.
        """
        summary = summary.strip()

        if len(summary) > backend.get_active_backend().summary_max_length:
            raise serializers.ValidationError(
                {
                    "summary": _(
                        "The length of the summary field exceeds the allowed limit of %s."
                    )
                    % backend.get_active_backend().summary_max_length
                }
            )

        return summary

    def validate_customer(self, customer: structure_models.Customer):
        """User has to be customer owner, staff or global support"""
        if not customer:
            return customer
        user = self.context["request"].user
        if (
            not customer
            or user.is_staff
            or user.is_support
            or customer.has_user(user, CustomerRole.OWNER)
        ):
            return customer
        raise serializers.ValidationError(
            _("Only customer owner, staff or support can report customer issues.")
        )

    def validate_project(self, project: structure_models.Project):
        if not project:
            return project
        user = self.context["request"].user
        if (
            not project
            or user.is_staff
            or user.is_support
            or project.customer.has_user(user, CustomerRole.OWNER)
            or project.has_user(user, ProjectRole.MANAGER)
            or project.has_user(user, ProjectRole.ADMIN)
            or project.has_user(user, ProjectRole.MEMBER)
        ):
            return project
        raise serializers.ValidationError(
            _(
                "Only customer owner, project manager, project admin, project support, staff or support can report such issue."
            )
        )

    def validate_resource(self, resource):
        if resource:
            self.validate_project(resource.project)
        return resource

    def validate_priority(self, priority):
        user = self.context["request"].user
        if user.is_authenticated and not user.is_staff and not user.is_support:
            raise serializers.ValidationError(
                _("Only staff or support can specify issue priority.")
            )
        try:
            models.Priority.objects.get(name=priority)
        except (models.Priority.DoesNotExist, models.Priority.MultipleObjectsReturned):
            raise serializers.ValidationError(
                _("Priority with requested name does not exist.")
            )
        return priority

    @transaction.atomic()
    def create(self, validated_data):
        resource = validated_data.get("resource")
        if resource:
            validated_data["project"] = resource.project
        project = validated_data.get("project")
        if project:
            validated_data["customer"] = project.customer

        rendered_description = render_issue_template(
            "ATLASSIAN_DESCRIPTION_TEMPLATE", "description", validated_data
        )

        impersonator = getattr(self.context["request"].user, "impersonator", None)

        if impersonator:
            rendered_description += f" \n\n\n\nImpersonator: {impersonator}"

        if backend.get_active_backend().message_format == backend.SupportedFormat.HTML:
            rendered_description = text2html(rendered_description)

        validated_data["description"] = rendered_description
        validated_data["summary"] = render_issue_template(
            "ATLASSIAN_SUMMARY_TEMPLATE", "summary", validated_data
        )
        return super().create(validated_data)

    def _render_template(self, config_name, issue):
        raw = self.issue_settings[config_name]
        template = Template(raw)
        return template.render(Context({"issue": issue}))


class PrioritySerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.Priority
        fields = ("url", "uuid", "name", "description", "icon_url")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }


class RequestTypeSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    url = serializers.HyperlinkedIdentityField(
        view_name="support-request-type-detail",
        lookup_field="uuid",
    )

    class Meta:
        model = models.RequestType
        fields = ("url", "uuid", "name", "issue_type_name", "order")


class RequestTypeAdminSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Admin serializer for managing request types with full CRUD."""

    url = serializers.HyperlinkedIdentityField(
        view_name="support-request-type-admin-detail",
        lookup_field="uuid",
    )
    is_synced = serializers.SerializerMethodField()

    class Meta:
        model = models.RequestType
        fields = (
            "url",
            "uuid",
            "name",
            "issue_type_name",
            "backend_id",
            "backend_name",
            "is_active",
            "order",
            "is_synced",
        )
        read_only_fields = ("backend_id", "backend_name", "is_synced")

    def get_is_synced(self, obj: models.RequestType) -> bool:
        """Returns True if the request type was synced from a backend."""
        return obj.backend_id is not None


class RequestTypeReorderItemSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    order = serializers.IntegerField()


class RequestTypeReorderSerializer(serializers.Serializer):
    items = RequestTypeReorderItemSerializer(many=True)


class CommentSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    # should be initialized with issue in context on creation
    author_user = serializers.HyperlinkedRelatedField(
        source="author.user",
        view_name="user-detail",
        lookup_field="uuid",
        read_only=True,
    )

    author_uuid = serializers.UUIDField(read_only=True, source="author.user.uuid")
    author_email = serializers.ReadOnlyField(source="author.user.email")
    author_image = serializers.ImageField(source="author.user.image", read_only=True)
    update_is_available = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()

    class Meta:
        model = models.Comment
        fields = (
            "url",
            "uuid",
            "issue",
            "issue_key",
            "description",
            "is_public",
            "author_name",
            "author_uuid",
            "author_user",
            "author_email",
            "author_image",
            "backend_id",
            "remote_id",
            "created",
            "update_is_available",
            "destroy_is_available",
        )
        read_only_fields = (
            "issue",
            "backend_id",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid"},
            issue={"lookup_field": "uuid", "view_name": "support-issue-detail"},
        )
        related_paths = dict(
            author=("name",),
            issue=("key",),
        )
        protected_fields = ("remote_id",)

    @cached_property
    def _active_backend(self):
        return backend.get_active_backend()

    def get_update_is_available(self, obj) -> bool:
        return self._active_backend.comment_update_is_available(obj)

    def get_destroy_is_available(self, obj) -> bool:
        return self._active_backend.comment_destroy_is_available(obj)

    def validate_description(self, description):
        impersonator = getattr(self.context["request"].user, "impersonator", None)

        if backend.get_active_backend().message_format == backend.SupportedFormat.HTML:
            description = text2html(description)

            if impersonator:
                description += f"<br/><br/>Impersonator: {impersonator}"
        else:
            if impersonator:
                description += f" /n/n/n/nImpersonator: {impersonator}"

        description = clean_html(description)

        return description

    @transaction.atomic()
    def create(self, validated_data):
        author_user = self.context["request"].user
        (
            validated_data["author"],
            _,
        ) = models.SupportUser.objects.get_or_create_from_user(author_user)
        validated_data["issue"] = self.context["view"].get_object()
        return super().create(validated_data)


class SupportUserSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    # SerializerMethodField, not source="user.full_name": a dotted source over a
    # null FK raises SkipField, which drops the key from the payload entirely
    # instead of returning null. Support users pulled from a backend commonly
    # have no linked user, so the field must stay present and nullable.
    user_full_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    reported_issues_count = serializers.SerializerMethodField()
    assigned_issues_count = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    attachments_count = serializers.SerializerMethodField()

    class Meta:
        model = models.SupportUser
        fields = (
            "url",
            "uuid",
            "name",
            "backend_id",
            "backend_name",
            "is_active",
            "user",
            "user_full_name",
            "user_email",
            "reported_issues_count",
            "assigned_issues_count",
            "comments_count",
            "attachments_count",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid"},
            user={
                "lookup_field": "uuid",
                "view_name": "user-detail",
                "allow_null": True,
                "required": False,
            },
        )

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_full_name(self, obj):
        return obj.user.full_name if obj.user else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_email(self, obj):
        return obj.user.email if obj.user else None

    @extend_schema_field(serializers.IntegerField())
    def get_reported_issues_count(self, obj):
        return obj.reported_issues.count()

    @extend_schema_field(serializers.IntegerField())
    def get_assigned_issues_count(self, obj):
        return obj.issues.count()

    @extend_schema_field(serializers.IntegerField())
    def get_comments_count(self, obj):
        return obj.comments.count()

    @extend_schema_field(serializers.IntegerField())
    def get_attachments_count(self, obj):
        return obj.attachments.count()


class SupportUserIssueBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Issue
        fields = ("uuid", "key", "type", "summary", "status", "created", "modified")


class SupportUserCommentBriefSerializer(serializers.ModelSerializer):
    issue_key = serializers.ReadOnlyField(source="issue.key")
    issue_uuid = serializers.ReadOnlyField(source="issue.uuid")

    class Meta:
        model = models.Comment
        fields = (
            "uuid",
            "description",
            "is_public",
            "created",
            "issue_key",
            "issue_uuid",
        )


class SupportUserAttachmentBriefSerializer(serializers.ModelSerializer):
    issue_key = serializers.ReadOnlyField(source="issue.key")
    issue_uuid = serializers.ReadOnlyField(source="issue.uuid")
    file_name = serializers.SerializerMethodField()

    class Meta:
        model = models.Attachment
        fields = ("uuid", "file_name", "created", "issue_key", "issue_uuid")

    @extend_schema_field(serializers.CharField())
    def get_file_name(self, obj):
        return obj.file.name.split("/")[-1] if obj.file else None


class SupportUserConnectionsSerializer(serializers.Serializer):
    """Objects a support user is connected to, for the management UI drill-down."""

    reported_issues = SupportUserIssueBriefSerializer(many=True, read_only=True)
    assigned_issues = SupportUserIssueBriefSerializer(many=True, read_only=True)
    comments = SupportUserCommentBriefSerializer(many=True, read_only=True)
    attachments = SupportUserAttachmentBriefSerializer(many=True, read_only=True)


class SupportUserMergeSerializer(serializers.Serializer):
    source_users = serializers.SlugRelatedField(
        slug_field="uuid",
        many=True,
        allow_empty=False,
        queryset=models.SupportUser.objects.all(),
        help_text="Support users to merge into this one. They will be deleted "
        "and their issues, comments and attachments re-pointed to this user.",
    )

    def validate_source_users(self, value):
        keeper = self.context["view"].get_object()
        if any(user.pk == keeper.pk for user in value):
            raise serializers.ValidationError(
                "A support user cannot be merged into itself."
            )
        return value


class JiraCommentSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="Jira comment ID")


class JiraChangelogSerializer(serializers.Serializer):
    items = serializers.ListField(help_text="List of changelog items")


class JiraFieldSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="Jira field ID")
    name = serializers.CharField(help_text="Jira field name")


class JiraIssueProjectSerializer(JiraFieldSerializer):
    key = serializers.CharField(help_text="Jira project key")


class JiraIssueFieldsSerializer(serializers.Serializer):
    project = JiraIssueProjectSerializer()
    comment = serializers.DictField(required=False)


class JiraIssueSerializer(serializers.Serializer):
    key = serializers.CharField(help_text="Jira issue key")
    fields = JiraIssueFieldsSerializer()


class WebHookReceiverSerializer(serializers.Serializer):
    webhookEvent = serializers.CharField()
    issue = JiraIssueSerializer()
    comment = JiraCommentSerializer(required=False)
    changelog = JiraChangelogSerializer(required=False)
    issue_event_type_name = serializers.CharField(
        required=False
    )  # For old Jira's version

    def create(self, validated_data):
        logger.debug("Processing webhook with data: %s", validated_data)

        webhook_event = validated_data["webhookEvent"]
        if webhook_event not in JIRA_WEBHOOK_EVENT_MAP:
            raise serializers.ValidationError(
                f"Unknown webhook event type: {webhook_event}"
            )

        event_type = JIRA_WEBHOOK_EVENT_MAP[webhook_event]
        logger.info("Processing webhook event type: %s", event_type)

        key = validated_data["issue"]["key"]
        logger.debug("Processing issue key: %s", key)

        backend = ServiceDeskBackend()
        issue: models.Issue = self.get_issue(key)
        logger.info("Loaded issue %s from database", issue)

        if event_type == SupportWebhookEvent.ISSUE_DELETE:
            logger.info("Processing issue deletion for key: %s", key)
            backend.delete_issue_from_jira(issue)
        else:
            # For all other events (issue updates, comment actions),
            # perform a full sync to ensure consistency
            logger.info("Performing full sync for issue: %s", key)
            backend.sync_single_issue(issue)

        logger.debug("Webhook processing completed for issue: %s", key)
        return validated_data

    def get_issue(self, key):
        try:
            issue = models.Issue.objects.get(backend_id=key)
        except models.Issue.DoesNotExist:
            raise serializers.ValidationError("Issue with id %s does not exist." % key)

        return issue


class AttachmentSerializer(
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    file_name = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()

    class Meta:
        model = models.Attachment
        fields = (
            "url",
            "uuid",
            "issue",
            "issue_key",
            "created",
            "file",
            "mime_type",
            "file_size",
            "file_name",
            "backend_id",
            "destroy_is_available",
        )
        read_only_fields = (
            "mime_type",
            "file_size",
            "file_name",
            "backend_id",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid"},
            issue={"lookup_field": "uuid", "view_name": "support-issue-detail"},
        )
        related_paths = dict(
            issue=("key",),
        )

    def get_file_name(self, attachment) -> str:
        _, file_name = os.path.split(attachment.file.name)
        return file_name

    def get_destroy_is_available(self, obj) -> bool:
        return backend.get_active_backend().attachment_destroy_is_available(obj)

    def validate(self, attrs):
        filename, file_extension = os.path.splitext(attrs["file"].name)

        if file_extension in config.ATLASSIAN_EXCLUDED_ATTACHMENT_TYPES:
            raise serializers.ValidationError(_("Invalid file extension"))

        user = self.context["request"].user
        author_user = self.context["request"].user
        (
            attrs["author"],
            created,
        ) = models.SupportUser.objects.get_or_create_from_user(author_user)

        issue = attrs["issue"]

        if not backend.get_active_backend().attachment_create_is_available(issue):
            raise serializers.ValidationError(_("Adding attachments is not available."))

        if (
            user.is_staff
            or (issue.customer and issue.customer.has_user(user, CustomerRole.OWNER))
            or issue.caller == user
        ):
            return attrs

        raise exceptions.PermissionDenied()


class CreateAttachmentsSerializer(serializers.Serializer):
    attachments = serializers.ListSerializer(
        child=serializers.FileField(), help_text="List of files to attach"
    )


class TemplateAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.TemplateAttachment
        fields = ("uuid", "name", "file", "mime_type", "file_size", "created")


class TemplateSerializer(serializers.HyperlinkedModelSerializer):
    attachments = TemplateAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = models.Template
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "issue_type",
            "attachments",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "support-template-detail"},
        )


class CreateFeedbackSerializer(serializers.HyperlinkedModelSerializer):
    token = serializers.CharField(required=True, write_only=True)

    class Meta:
        model = models.Feedback
        fields = (
            "uuid",
            "issue",
            "comment",
            "evaluation",
            "token",
        )

        read_only_fields = ("issue",)
        extra_kwargs = dict(
            issue={"lookup_field": "uuid", "view_name": "support-issue-detail"},
        )

    def validate(self, attrs):
        token = attrs.pop("token")
        signer = signing.TimestampSigner()
        try:
            issue_uuid = signer.unsign(
                token, max_age=timedelta(days=settings.ISSUE_FEEDBACK_TOKEN_PERIOD)
            )

            if not is_uuid_like(issue_uuid):
                raise serializers.ValidationError(
                    {"token": _("UUID:%s is not valid.") % issue_uuid}
                )

            issue = models.Issue.objects.get(uuid=issue_uuid)

            if models.Feedback.objects.filter(issue=issue).exists():
                raise serializers.ValidationError(
                    _("Feedback for this issue already exists.")
                )
        except signing.BadSignature:
            raise serializers.ValidationError({"token": _("Token is wrong.")})
        except models.Issue.DoesNotExist:
            raise serializers.ValidationError(_("An issue is not found."))

        attrs["issue"] = issue
        return attrs


class FeedbackSerializer(serializers.HyperlinkedModelSerializer):
    issue_uuid = serializers.UUIDField(read_only=True, source="issue.uuid")
    issue_key = serializers.ReadOnlyField(source="issue.key")
    user_full_name = serializers.ReadOnlyField(source="issue.caller.full_name")
    issue_summary = serializers.ReadOnlyField(source="issue.summary")
    state = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=CoreStates.labels))
    def get_state(self, obj):
        return obj.get_state_display()

    class Meta:
        model = models.Feedback
        fields = (
            "uuid",
            "created",
            "modified",
            "state",
            "evaluation",
            "comment",
            "issue_uuid",
            "user_full_name",
            "issue_key",
            "issue_summary",
        )


class EscalateIssueSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, help_text="Reason for escalation.")


class AttachResourceSerializer(serializers.Serializer):
    resource = core_serializers.GenericRelatedField(
        related_models=structure_models.BaseResource.get_all_models()
        + [marketplace_models.Resource],
        required=True,
        help_text="URL of the marketplace resource to attach to this issue.",
    )


class RouteToProviderSerializer(serializers.Serializer):
    provider_helpdesk = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ProviderHelpdesk.objects.filter(is_active=True),
        help_text="UUID of the provider helpdesk to route this issue to.",
    )


class ProviderHelpdeskSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    service_provider = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=marketplace_models.ServiceProvider.objects.all(),
    )
    service_provider_name = serializers.ReadOnlyField(
        source="service_provider.customer.name"
    )
    health_status = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField())
    def get_health_status(self, obj):
        return obj.health_status

    class Meta:
        model = models.ProviderHelpdesk
        fields = (
            "url",
            "uuid",
            "service_provider",
            "service_provider_name",
            "backend_type",
            "settings",
            "is_active",
            "webhook_secret",
            "notification_email",
            "notify_on_new_ticket",
            "notify_on_comment",
            "notify_on_escalation",
            "notify_on_sla_warning",
            "health_status",
            "last_health_check",
            "failed_routing_count",
            "created",
            "modified",
        )
        read_only_fields = (
            "health_status",
            "last_health_check",
            "failed_routing_count",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "provider-helpdesk-detail",
            },
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Mask webhook secret in output
        if data.get("webhook_secret"):
            data["webhook_secret"] = "***"
        return data


class ProviderTicketSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    parent_issue_key = serializers.ReadOnlyField(source="parent_issue.key")
    parent_issue_uuid = serializers.ReadOnlyField(source="parent_issue.uuid")
    provider_assignee_name = serializers.ReadOnlyField(
        source="provider_assignee.user.full_name"
    )
    provider_assignee = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )

    class Meta:
        model = models.Issue
        fields = (
            "url",
            "uuid",
            "key",
            "summary",
            "description",
            "type",
            "status",
            "priority",
            "created",
            "modified",
            "parent_issue_key",
            "parent_issue_uuid",
            "is_escalated",
            "escalated_at",
            "provider_assignee",
            "provider_assignee_name",
            "first_response_deadline",
            "resolution_deadline",
            "first_response_at",
            "sla_breached",
            "customer",
            "customer_uuid",
            "customer_name",
            "project",
            "project_uuid",
            "project_name",
        )
        read_only_fields = (
            "key",
            "summary",
            "description",
            "type",
            "status",
            "priority",
            "parent_issue_key",
            "parent_issue_uuid",
            "is_escalated",
            "first_response_deadline",
            "resolution_deadline",
            "first_response_at",
            "sla_breached",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "provider-ticket-detail"},
            customer={"lookup_field": "uuid", "view_name": "customer-detail"},
            project={"lookup_field": "uuid", "view_name": "project-detail"},
        )
        related_paths = dict(
            customer=("uuid", "name"),
            project=("uuid", "name"),
        )


class ProviderCommentSerializer(serializers.Serializer):
    description = serializers.CharField(required=True)
    is_public = serializers.BooleanField(default=True)


class ProviderAssignSerializer(serializers.Serializer):
    provider_support_user = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ProviderSupportUser.objects.all(),
    )


class CallerContextSerializer(serializers.Serializer):
    full_name = serializers.CharField()
    email = serializers.EmailField()
    organization = serializers.CharField(allow_blank=True)


class ResourceContextSerializer(serializers.Serializer):
    name = serializers.CharField()
    type = serializers.CharField()


class RecentTicketSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    key = serializers.CharField()
    summary = serializers.CharField()
    status = serializers.CharField()
    created = serializers.DateTimeField()


class CustomerContextSerializer(serializers.Serializer):
    caller = CallerContextSerializer()
    resource = ResourceContextSerializer(allow_null=True)
    recent_tickets = RecentTicketSerializer(many=True)


class ProviderSupportUserSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    user = serializers.HyperlinkedRelatedField(
        view_name="user-detail",
        lookup_field="uuid",
        queryset=User.objects.all(),
    )
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_email = serializers.ReadOnlyField(source="user.email")
    provider_helpdesk = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ProviderHelpdesk.objects.all(),
    )
    open_ticket_count = serializers.SerializerMethodField()
    has_capacity = serializers.SerializerMethodField()

    @extend_schema_field(serializers.IntegerField())
    def get_open_ticket_count(self, obj):
        return obj.open_ticket_count

    @extend_schema_field(serializers.BooleanField())
    def get_has_capacity(self, obj):
        return obj.has_capacity

    class Meta:
        model = models.ProviderSupportUser
        fields = (
            "url",
            "uuid",
            "user",
            "user_full_name",
            "user_email",
            "provider_helpdesk",
            "role",
            "is_active",
            "skills",
            "max_open_tickets",
            "open_ticket_count",
            "has_capacity",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "provider-support-user-detail",
            },
        }


class TeamWorkloadSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    user_full_name = serializers.CharField()
    open_ticket_count = serializers.IntegerField()
    max_open_tickets = serializers.IntegerField()
    has_capacity = serializers.BooleanField()


class ProviderCannedResponseSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    provider_helpdesk = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ProviderHelpdesk.objects.all(),
    )

    class Meta:
        model = models.ProviderCannedResponse
        fields = (
            "url",
            "uuid",
            "name",
            "provider_helpdesk",
            "text",
            "category",
            "usage_count",
            "created",
            "modified",
        )
        read_only_fields = ("usage_count",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "provider-canned-response-detail",
            },
        }


class ProviderStatsSerializer(serializers.Serializer):
    total_open = serializers.IntegerField()
    total_resolved = serializers.IntegerField()
    total_escalated = serializers.IntegerField()
    sla_breach_count = serializers.IntegerField()
    avg_resolution_hours = serializers.FloatField(allow_null=True)
    by_status = serializers.DictField()


class IssueTagSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.IssueTag
        fields = ("url", "uuid", "name", "color")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "support-issue-tag-detail"},
        }


class IssueLinkSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    source = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Issue.objects.all()
    )
    target = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Issue.objects.all()
    )
    source_key = serializers.ReadOnlyField(source="source.key")
    target_key = serializers.ReadOnlyField(source="target.key")

    class Meta:
        model = models.IssueLink
        fields = (
            "url",
            "uuid",
            "source",
            "source_key",
            "target",
            "target_key",
            "link_type",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "support-issue-link-detail"},
        }


class SavedFilterSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.SavedFilter
        fields = (
            "url",
            "uuid",
            "name",
            "filter_params",
            "is_shared",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "support-saved-filter-detail"},
        }

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class CannedResponseSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.CannedResponse
        fields = (
            "url",
            "uuid",
            "name",
            "text",
            "category",
            "is_active",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "support-canned-response-detail",
            },
        }

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class CannedResponseRenderSerializer(serializers.Serializer):
    context = serializers.DictField(required=False, default=dict)


class BulkUpdateIssueSerializer(serializers.Serializer):
    issue_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="List of issue UUIDs to update.",
    )
    status = serializers.CharField(required=False)
    priority = serializers.CharField(required=False)
    assignee = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.SupportUser.objects.all(),
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        if not any(k in attrs for k in ("status", "priority", "assignee")):
            raise serializers.ValidationError(
                "At least one of status, priority, or assignee must be provided."
            )
        return attrs


class HelpdeskStatsSerializer(serializers.Serializer):
    total_open = serializers.IntegerField()
    total_closed_this_month = serializers.IntegerField()
    total_routed = serializers.IntegerField()
    total_escalated = serializers.IntegerField()
    sla_breach_count = serializers.IntegerField()
    avg_first_response_hours = serializers.FloatField(allow_null=True)
    avg_resolution_hours = serializers.FloatField(allow_null=True)
    by_status = serializers.DictField()
    by_priority = serializers.DictField()


class WebhookPayloadSerializer(serializers.Serializer):
    event_type = serializers.CharField()
    issue_backend_id = serializers.CharField(required=False)
    comment = serializers.CharField(required=False)
    new_status = serializers.CharField(required=False)


class HelpdeskHealthSerializer(serializers.Serializer):
    provider_name = serializers.CharField()
    backend_type = serializers.CharField()
    is_active = serializers.BooleanField()
    health_status = serializers.CharField()
    last_health_check = serializers.DateTimeField(allow_null=True)
    failed_routing_count = serializers.IntegerField()


class SupportStatsSerializer(serializers.Serializer):
    open_issues_count = serializers.IntegerField(read_only=True)
    closed_this_month_count = serializers.IntegerField(read_only=True)
    recent_broadcasts_count = serializers.IntegerField(read_only=True)


class DeleteAttachmentsSerializer(serializers.Serializer):
    attachment_ids = serializers.ListField(child=serializers.UUIDField())


class IssueStatusSerializer(serializers.HyperlinkedModelSerializer):
    type_display = serializers.SerializerMethodField()

    class Meta:
        model = models.IssueStatus
        fields = ("url", "uuid", "name", "type", "type_display")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "support-issue-status-detail"},
        }

    def get_type_display(self, obj) -> str:
        return obj.get_type_display()


class IssueStatusCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating and updating IssueStatus entries."""

    class Meta:
        model = models.IssueStatus
        fields = ("name", "type")

    def validate_name(self, value):
        """Ensure name is unique (case-insensitive check for better UX)."""
        queryset = models.IssueStatus.objects.filter(name__iexact=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                "Issue status with this name already exists."
            )
        return value


class SmaxWebHookReceiverSerializer(serializers.Serializer):
    id = serializers.CharField()


# ==========================================
# Atlassian Settings Discovery Serializers
# ==========================================


class AtlassianCredentialsSerializer(serializers.Serializer):
    """Serializer for Atlassian credentials - accepts temporary credentials."""

    api_url = serializers.URLField(
        required=True,
        help_text="Atlassian API URL (e.g., https://your-domain.atlassian.net)",
    )
    auth_method = serializers.ChoiceField(
        choices=[
            ("api_token", "API Token (Cloud)"),
            ("personal_access_token", "Personal Access Token (Server)"),
            ("basic", "Basic Authentication"),
        ],
        required=True,
        help_text="Authentication method to use",
    )

    # API Token authentication (Cloud)
    email = serializers.EmailField(required=False, allow_blank=True)
    token = serializers.CharField(required=False, allow_blank=True, write_only=True)

    # Personal Access Token authentication (Server)
    personal_access_token = serializers.CharField(
        required=False, allow_blank=True, write_only=True
    )

    # Basic authentication
    username = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    # Optional SSL verification toggle
    verify_ssl = serializers.BooleanField(default=True)

    def validate(self, attrs):
        auth_method = attrs.get("auth_method")

        if auth_method == "api_token":
            if not attrs.get("email") or not attrs.get("token"):
                raise serializers.ValidationError(
                    {
                        "email": "Email is required for API Token authentication",
                        "token": "Token is required for API Token authentication",
                    }
                )
        elif auth_method == "personal_access_token":
            if not attrs.get("personal_access_token"):
                raise serializers.ValidationError(
                    {"personal_access_token": "Personal Access Token is required"}
                )
        elif auth_method == "basic":
            if not attrs.get("username") or not attrs.get("password"):
                raise serializers.ValidationError(
                    {
                        "username": "Username is required for Basic authentication",
                        "password": "Password is required for Basic authentication",
                    }
                )

        return attrs


class DiscoverProjectsRequestSerializer(AtlassianCredentialsSerializer):
    """Request serializer for project discovery - credentials only."""

    pass


class DiscoverRequestTypesRequestSerializer(AtlassianCredentialsSerializer):
    """Request serializer for request type discovery."""

    project_id = serializers.CharField(
        required=True, help_text="Service Desk project ID or key"
    )


class DiscoverCustomFieldsRequestSerializer(AtlassianCredentialsSerializer):
    """Request serializer for custom field discovery."""

    project_id = serializers.CharField(required=False, allow_blank=True)
    request_type_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional: Filter fields by request type",
    )


class DiscoverPrioritiesRequestSerializer(AtlassianCredentialsSerializer):
    """Request serializer for priority discovery - credentials only."""

    pass


# Response serializers for discovered data


class AtlassianProjectResponseSerializer(serializers.Serializer):
    """Response serializer for discovered projects."""

    id = serializers.CharField()
    key = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False)


class AtlassianRequestTypeResponseSerializer(serializers.Serializer):
    """Response serializer for discovered request types."""

    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False)
    issue_type_id = serializers.CharField(required=False)


class AtlassianCustomFieldResponseSerializer(serializers.Serializer):
    """Response serializer for discovered custom fields."""

    id = serializers.CharField()
    name = serializers.CharField()
    clause_names = serializers.ListField(child=serializers.CharField(), required=False)
    field_type = serializers.CharField(required=False)
    required = serializers.BooleanField(default=False)


class AtlassianPriorityResponseSerializer(serializers.Serializer):
    """Response serializer for discovered priorities."""

    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False)
    icon_url = serializers.URLField(required=False, allow_blank=True)


# Preview and Save serializers


class AtlassianSettingsPreviewSerializer(serializers.Serializer):
    """Request serializer for previewing settings to be saved."""

    # Credentials (inline, not nested for easier API usage)
    api_url = serializers.URLField(required=True)
    auth_method = serializers.ChoiceField(
        choices=[
            ("api_token", "API Token (Cloud)"),
            ("personal_access_token", "Personal Access Token (Server)"),
            ("basic", "Basic Authentication"),
        ],
        required=True,
    )
    email = serializers.EmailField(required=False, allow_blank=True)
    token = serializers.CharField(required=False, allow_blank=True, write_only=True)
    personal_access_token = serializers.CharField(
        required=False, allow_blank=True, write_only=True
    )
    username = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)
    verify_ssl = serializers.BooleanField(default=True)

    # Selected configuration
    project_id = serializers.CharField(required=True)
    issue_types = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    support_type_mapping = serializers.DictField(
        child=serializers.CharField(),
        required=False,
        help_text="Mapping from frontend types to backend request types",
    )

    # Custom field mappings (field name in Atlassian)
    reporter_field = serializers.CharField(required=False, allow_blank=True)
    impact_field = serializers.CharField(required=False, allow_blank=True)
    organisation_field = serializers.CharField(required=False, allow_blank=True)
    project_field = serializers.CharField(required=False, allow_blank=True)
    affected_resource_field = serializers.CharField(required=False, allow_blank=True)
    caller_field = serializers.CharField(required=False, allow_blank=True)
    template_field = serializers.CharField(required=False, allow_blank=True)
    sla_field = serializers.CharField(required=False, allow_blank=True)
    resolution_sla_field = serializers.CharField(required=False, allow_blank=True)
    satisfaction_field = serializers.CharField(required=False, allow_blank=True)
    request_feedback_field = serializers.CharField(required=False, allow_blank=True)
    waldur_backend_id_field = serializers.CharField(required=False, allow_blank=True)
    default_offering_issue_type = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Default issue type for marketplace request-based orders",
    )

    # Options
    use_old_api = serializers.BooleanField(default=False)
    custom_field_mapping_enabled = serializers.BooleanField(default=True)

    def validate(self, attrs):
        auth_method = attrs.get("auth_method")

        if auth_method == "api_token":
            if not attrs.get("email") or not attrs.get("token"):
                raise serializers.ValidationError(
                    {
                        "email": "Email is required for API Token authentication",
                        "token": "Token is required for API Token authentication",
                    }
                )
        elif auth_method == "personal_access_token":
            if not attrs.get("personal_access_token"):
                raise serializers.ValidationError(
                    {"personal_access_token": "Personal Access Token is required"}
                )
        elif auth_method == "basic":
            if not attrs.get("username") or not attrs.get("password"):
                raise serializers.ValidationError(
                    {
                        "username": "Username is required for Basic authentication",
                        "password": "Password is required for Basic authentication",
                    }
                )

        return attrs


class AtlassianSettingsSaveSerializer(AtlassianSettingsPreviewSerializer):
    """Request serializer for saving settings to constance."""

    confirm_save = serializers.BooleanField(
        required=True, help_text="Must be True to confirm saving settings"
    )

    def validate_confirm_save(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must set confirm_save to True to save settings."
            )
        return value


def get_has_active_helpdesk(serializer, customer) -> bool:
    # Lets the UI hide the provider Helpdesk workspace tab for providers that
    # have not configured a helpdesk yet. Only meaningful when provider routing
    # is enabled, so the common case costs no query.
    if not config.WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED:
        return False
    return models.ProviderHelpdesk.objects.filter(
        service_provider__customer=customer, is_active=True
    ).exists()


def add_has_active_helpdesk(sender, fields, **kwargs):
    """Add a flag telling whether the customer's provider has an active helpdesk."""
    fields["has_active_helpdesk"] = serializers.SerializerMethodField()
    setattr(sender, "get_has_active_helpdesk", get_has_active_helpdesk)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_has_active_helpdesk,
)
