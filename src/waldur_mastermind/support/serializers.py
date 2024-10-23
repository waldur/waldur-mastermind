import logging
import os
from datetime import timedelta

from constance import config
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.template import Context, Template
from django.template import exceptions as template_exceptions
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core.clean_html import clean_html
from waldur_core.core.utils import is_uuid_like, text2html
from waldur_core.structure import models as structure_models
from waldur_core.structure.registry import get_resource_type
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend

from . import backend, models, utils

logger = logging.getLogger(__name__)
User = get_user_model()


def render_issue_template(config_name, template_name, issue):
    try:
        template = get_template("support/" + template_name + ".txt").template
    except template_exceptions.TemplateDoesNotExist:
        raw = getattr(config, config_name)
        template = Template(raw)

    return template.render(
        Context({"issue": issue, "settings": settings}, autoescape=False)
    )


class NestedFeedbackSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField(source="get_state_display")
    evaluation = serializers.ReadOnlyField(source="get_evaluation_display")
    evaluation_number = serializers.ReadOnlyField(source="evaluation")

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
    resource_type = serializers.SerializerMethodField()
    resource_name = serializers.ReadOnlyField(source="resource.name")
    type = serializers.ChoiceField(
        choices=[
            (t.strip(), t.strip()) for t in config.ATLASSIAN_ISSUE_TYPES.split(",")
        ],
        initial=utils.get_atlassian_issue_type(),
        default=utils.get_atlassian_issue_type(),
    )
    is_reported_manually = serializers.BooleanField(
        initial=False,
        default=False,
        write_only=True,
        help_text=_("Set true if issue is created by regular user via portal."),
    )
    feedback = NestedFeedbackSerializer(required=False, read_only=True)
    update_is_available = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()
    add_comment_is_available = serializers.SerializerMethodField()
    add_attachment_is_available = serializers.SerializerMethodField()

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
            "first_response_sla",
            "template",
            "feedback",
            "resolved",
            "update_is_available",
            "destroy_is_available",
            "add_comment_is_available",
            "add_attachment_is_available",
        )
        read_only_fields = (
            "key",
            "status",
            "resolution",
            "backend_id",
            "backend_name",
            "link",
            "first_response_sla",
            "feedback",
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

        if (
            "view" not in self.context
        ):  # On docs generation context does not contain "view".
            return fields

        user = self.context["view"].request.user
        if not user.is_staff and not user.is_support:
            del fields["link"]

        return fields

    def get_resource_type(self, obj):
        if isinstance(obj.resource, structure_models.BaseResource):
            return get_resource_type(obj.resource_content_type.model_class())
        if isinstance(obj.resource, marketplace_models.Resource):
            return "Marketplace.Resource"

    def get_update_is_available(self, obj):
        return backend.get_active_backend().update_is_available(obj)

    def get_destroy_is_available(self, obj):
        return backend.get_active_backend().destroy_is_available(obj)

    def get_add_comment_is_available(self, obj):
        return backend.get_active_backend().comment_create_is_available(obj)

    def get_add_attachment_is_available(self, obj):
        return backend.get_active_backend().attachment_create_is_available(obj)

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

    def validate_customer(self, customer):
        """User has to be customer owner, staff or global support"""
        if not customer:
            return customer
        user = self.context["request"].user
        if (
            not customer
            or user.is_staff
            or user.is_support
            or customer.has_user(user, structure_models.CustomerRole.OWNER)
        ):
            return customer
        raise serializers.ValidationError(
            _("Only customer owner, staff or support can report customer issues.")
        )

    def validate_project(self, project):
        if not project:
            return project
        user = self.context["request"].user
        if (
            not project
            or user.is_staff
            or user.is_support
            or project.customer.has_user(user, structure_models.CustomerRole.OWNER)
            or project.has_user(user, structure_models.ProjectRole.MANAGER)
            or project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR)
            or project.has_user(user, structure_models.ProjectRole.MEMBER)
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
        if not user.is_staff and not user.is_support:
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

    author_uuid = serializers.ReadOnlyField(source="author.user.uuid")
    author_email = serializers.ReadOnlyField(source="author.user.email")
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

    def get_update_is_available(self, obj):
        return backend.get_active_backend().comment_update_is_available(obj)

    def get_destroy_is_available(self, obj):
        return backend.get_active_backend().comment_destroy_is_available(obj)

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
    class Meta:
        model = models.SupportUser
        fields = ("url", "uuid", "name", "backend_id", "user", "backend_name")
        extra_kwargs = dict(
            url={"lookup_field": "uuid"},
            user={"lookup_field": "uuid", "view_name": "user-detail"},
        )


class JiraCommentSerializer(serializers.Serializer):
    id = serializers.CharField()


class JiraChangelogSerializer(serializers.Serializer):
    items = serializers.ListField()


class JiraFieldSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()


class JiraIssueProjectSerializer(JiraFieldSerializer):
    key = serializers.CharField()


class JiraIssueFieldsSerializer(serializers.Serializer):
    project = JiraIssueProjectSerializer()
    comment = serializers.DictField(required=False)


class JiraIssueSerializer(serializers.Serializer):
    key = serializers.CharField()
    fields = JiraIssueFieldsSerializer()


class WebHookReceiverSerializer(serializers.Serializer):
    class Event:
        ISSUE_UPDATE = 2
        ISSUE_DELETE = 4
        COMMENT_CREATE = 5
        COMMENT_UPDATE = 6
        COMMENT_DELETE = 7

        ISSUE_ACTIONS = (ISSUE_UPDATE, ISSUE_DELETE)
        COMMENT_ACTIONS = (COMMENT_CREATE, COMMENT_UPDATE, COMMENT_DELETE)

        CHOICES = {
            ("jira:issue_updated", ISSUE_UPDATE),
            ("jira:issue_deleted", ISSUE_DELETE),
            ("comment_created", COMMENT_CREATE),
            ("comment_updated", COMMENT_UPDATE),
            ("comment_deleted", COMMENT_DELETE),
        }

    webhookEvent = serializers.ChoiceField(choices=Event.CHOICES)
    issue = JiraIssueSerializer()
    comment = JiraCommentSerializer(required=False)
    changelog = JiraChangelogSerializer(required=False)
    issue_event_type_name = serializers.CharField(
        required=False
    )  # For old Jira's version

    def create(self, validated_data):
        event_type = dict(self.Event.CHOICES).get(validated_data["webhookEvent"])
        fields = validated_data["issue"]["fields"]
        key = validated_data["issue"]["key"]
        backend = ServiceDeskBackend()
        issue = self.get_issue(key)

        if fields.get("comment", False):
            # The processing of hooks requests for the old and new Jira versions is different.
            # The main difference is that in the old version, when changing comments,
            # jira:issue_updated event is sent to the new comment_X event.
            old_jira = validated_data.get("issue_event_type_name", True)
        else:
            old_jira = False

        if event_type == self.Event.ISSUE_UPDATE:
            if old_jira:
                if old_jira == "issue_commented":
                    comment_backend_id = validated_data["comment"]["id"]
                    backend.create_comment_from_jira(issue, comment_backend_id)

                if old_jira == "issue_comment_edited":
                    comment_backend_id = validated_data["comment"]["id"]
                    comment = self.get_comment(issue, comment_backend_id, False)
                    backend.update_comment_from_jira(comment)

                if old_jira == "issue_comment_deleted":
                    backend.delete_old_comments(issue)

                if old_jira in ("issue_updated", "issue_generic"):
                    items = validated_data["changelog"]["items"]
                    if any(item["field"] == "Attachment" for item in items):
                        backend.update_attachment_from_jira(issue)

                    backend.update_issue_from_jira(issue)

            else:
                backend.update_issue_from_jira(issue)
                backend.update_attachment_from_jira(issue)

        elif event_type == self.Event.ISSUE_DELETE:
            backend.delete_issue_from_jira(issue)

        elif event_type in self.Event.COMMENT_ACTIONS:
            try:
                comment_backend_id = validated_data["comment"]["id"]
            except KeyError:
                raise serializers.ValidationError(
                    "Request not include fields.comment.id"
                )

            create_comment = event_type == self.Event.COMMENT_CREATE
            comment = self.get_comment(issue, comment_backend_id, create_comment)

            if not comment and create_comment:
                backend.create_comment_from_jira(issue, comment_backend_id)
                backend.update_attachment_from_jira(issue)

            if event_type == self.Event.COMMENT_UPDATE:
                backend.update_comment_from_jira(comment)
                backend.update_attachment_from_jira(issue)

            if event_type == self.Event.COMMENT_DELETE:
                backend.delete_comment_from_jira(comment)
                backend.update_attachment_from_jira(issue)

        return validated_data

    def get_issue(self, key):
        issue = None

        try:
            issue = models.Issue.objects.get(backend_id=key)
        except models.Issue.DoesNotExist:
            raise serializers.ValidationError("Issue with id %s does not exist." % key)

        return issue

    def get_comment(self, issue, key, create):
        comment = None

        try:
            comment = models.Comment.objects.get(issue=issue, backend_id=key)
        except models.Comment.DoesNotExist:
            if not create:
                raise serializers.ValidationError(
                    "Comment with id %s does not exist." % key
                )

        return comment


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

    def get_file_name(self, attachment):
        _, file_name = os.path.split(attachment.file.name)
        return file_name

    def get_destroy_is_available(self, obj):
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
            or (
                issue.customer
                and issue.customer.has_user(user, structure_models.CustomerRole.OWNER)
            )
            or issue.caller == user
        ):
            return attrs

        raise exceptions.PermissionDenied()


class TemplateAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.TemplateAttachment
        fields = ("name", "file")


class TemplateSerializer(serializers.HyperlinkedModelSerializer):
    attachments = TemplateAttachmentSerializer(many=True)

    class Meta:
        model = models.Template
        fields = (
            "url",
            "uuid",
            "name",
            "native_name",
            "description",
            "native_description",
            "issue_type",
            "attachments",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "support-template-detail"},
        )

    def get_fields(self):
        fields = super().get_fields()
        if not settings.WALDUR_CORE["NATIVE_NAME_ENABLED"]:
            del fields["native_name"]
            del fields["native_description"]
        return fields


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
    issue_uuid = serializers.ReadOnlyField(source="issue.uuid")
    issue_key = serializers.ReadOnlyField(source="issue.key")
    user_full_name = serializers.ReadOnlyField(source="issue.caller.full_name")
    issue_summary = serializers.ReadOnlyField(source="issue.summary")

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
