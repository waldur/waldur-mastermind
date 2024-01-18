import collections
import json
import logging
from datetime import datetime
from html import unescape

import dateutil.parser
from constance import config
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.template import Context, Template
from django.utils import timezone
from jira import Comment, JIRAError
from jira.utils import json_loads

from waldur_core.structure.exceptions import ServiceBackendError
from waldur_jira.backend import JiraBackend, JiraBackendError, reraise_exceptions
from waldur_mastermind.support import models
from waldur_mastermind.support.exceptions import SupportUserInactive

from . import SupportBackend

logger = logging.getLogger(__name__)

Settings = collections.namedtuple(
    "Settings", ["backend_url", "username", "password", "email", "token"]
)


class ServiceDeskBackend(JiraBackend, SupportBackend):
    servicedeskapi_path = "servicedeskapi"
    model_comment = models.Comment
    model_issue = models.Issue
    model_attachment = models.Attachment
    backend_name = "atlassian"

    def __init__(self):
        self.settings = Settings(
            backend_url=config.ATLASSIAN_API_URL,
            username=config.ATLASSIAN_USERNAME,
            password=config.ATLASSIAN_PASSWORD,
            email=config.ATLASSIAN_EMAIL,
            token=config.ATLASSIAN_TOKEN,
        )
        self.verify = config.ATLASSIAN_VERIFY_SSL
        # allow to define reference by ID as older SD cannot properly resolve
        # TODO drop once transition to request API is complete
        self.use_old_api = config.ATLASSIAN_USE_OLD_API
        self.use_teenage_api = config.ATLASSIAN_USE_TEENAGE_API
        # In ideal world where Atlassian SD respects its spec the setting below would not be needed
        self.use_automatic_request_mapping = (
            config.ATLASSIAN_USE_AUTOMATIC_REQUEST_MAPPING
        )
        # In some cases list of priorities available to customers differ from the total list returned by SDK
        self.pull_priorities_automatically = config.ATLASSIAN_PULL_PRIORITIES
        self.strange_setting = config.ATLASSIAN_STRANGE_SETTING

    def pull_service_properties(self):
        super().pull_service_properties()
        self.pull_request_types()
        if self.pull_priorities_automatically:
            self.pull_priorities()

    @reraise_exceptions
    def create_comment(self, comment):
        backend_comment = self._add_comment(
            comment.issue.backend_id,
            comment.prepare_message(),
            is_internal=not comment.is_public,
        )
        comment.backend_id = backend_comment.id
        comment.save(update_fields=["backend_id"])

    def _add_comment(self, issue, body, is_internal):
        data = {
            "body": body,
            "properties": [
                {"key": "sd.public.comment", "value": {"internal": is_internal}},
            ],
        }

        url = self.manager._get_url(f"issue/{issue}/comment")
        response = self.manager._session.post(url, data=json.dumps(data))

        comment = Comment(
            self.manager._options, self.manager._session, raw=json_loads(response)
        )
        return comment

    @reraise_exceptions
    def create_issue(self, issue):
        if not issue.caller.email:
            raise ServiceBackendError(
                "Issue is not created because caller user does not have email."
            )

        self.create_user(issue.caller)

        args = self._issue_to_dict(issue)
        args["serviceDeskId"] = self.manager.waldur_service_desk(
            config.ATLASSIAN_PROJECT_ID
        )
        if not models.RequestType.objects.filter(issue_type_name=issue.type).count():
            self.pull_request_types()

        if not models.RequestType.objects.filter(issue_type_name=issue.type).count():
            raise ServiceBackendError(
                f"Issue is not created because request type is not found for issue type {issue.type}."
            )

        args["requestTypeId"] = (
            models.RequestType.objects.filter(issue_type_name=issue.type)
            .first()
            .backend_id
        )
        backend_issue = self.manager.waldur_create_customer_request(
            args, use_old_api=self.use_old_api
        )
        args = self._get_custom_fields(issue)

        try:
            # Update an issue, because create_customer_request doesn't allow setting custom fields.
            backend_issue.update(**args)
        except JIRAError as e:
            logger.error("Error when setting custom field via JIRA API: %s" % e)

        self._backend_issue_to_issue(backend_issue, issue)
        issue.save()

    @reraise_exceptions
    def create_confirmation_comment(self, issue, comment_tmpl=""):
        if not comment_tmpl:
            comment_tmpl = self.get_confirmation_comment_template(issue.type)

        if not comment_tmpl:
            return

        body = (
            Template(comment_tmpl)
            .render(Context({"issue": issue}, autoescape=False))
            .strip()
        )
        return self._add_comment(issue.backend_id, body, is_internal=False)

    def create_user(self, user):
        # Temporary workaround as JIRA returns 500 error if user already exists
        if self.use_old_api or self.use_teenage_api:
            # old API has a bug that causes user active status to be set to False if includeInactive is passed as True
            existing_support_user = self.manager.search_users(user.email)
        else:
            # user GDPR-compliant version of user search
            existing_support_user = self.manager.waldur_search_users(
                user.email, includeInactive=True
            )

        if existing_support_user:
            active_user = [u for u in existing_support_user if u.active]
            if not active_user:
                raise SupportUserInactive(
                    "Issue is not created because caller user is disabled."
                )

            logger.debug(
                "Skipping user %s creation because it already exists", user.email
            )
            backend_customer = active_user[0]
        else:
            if self.use_old_api:
                backend_customer = self.manager.waldur_create_customer(
                    user.email, user.full_name
                )
            else:
                backend_customer = self.manager.create_customer(
                    user.email, user.full_name
                )
        backend_id = self.get_user_id(backend_customer)
        try:
            user.supportcustomer
        except ObjectDoesNotExist:
            if models.SupportCustomer.objects.filter(backend_id=backend_id).exists():
                raise ServiceBackendError(
                    "Issue is not created because JIRA user with the same "
                    "email is already associated with another user."
                )
            support_customer = models.SupportCustomer(user=user, backend_id=backend_id)
            support_customer.save()

    @reraise_exceptions
    def get_users(self):
        users = self.manager.search_assignable_users_for_projects(
            "", config.ATLASSIAN_PROJECT_ID, maxResults=False
        )
        return [
            models.SupportUser(name=user.displayName, backend_id=self.get_user_id(user))
            for user in users
        ]

    def _get_custom_fields(self, issue):
        args = {}

        if issue.reporter:
            args[
                self.get_field_id_by_name(config.ATLASSIAN_REPORTER_FIELD)
            ] = issue.reporter.name
        if issue.impact:
            args[
                self.get_field_id_by_name(config.ATLASSIAN_IMPACT_FIELD)
            ] = issue.impact
        if issue.priority:
            args["priority"] = {"name": issue.priority}

        def set_custom_field(field_name, value):
            if value and getattr(config, field_name):
                args[self.get_field_id_by_name(getattr(config, field_name))] = value

        if issue.customer:
            set_custom_field("ATLASSIAN_ORGANISATION_FIELD", issue.customer.name)

        if issue.project:
            set_custom_field("ATLASSIAN_PROJECT_FIELD", issue.project.name)

        if issue.resource:
            set_custom_field("ATLASSIAN_AFFECTED_RESOURCE_FIELD", issue.resource)

        if issue.template:
            set_custom_field("ATLASSIAN_TEMPLATE_FIELD", issue.template.name)

        return args

    def _issue_to_dict(self, issue):
        args = {
            "requestFieldValues": {
                "summary": unescape(issue.summary),
                "description": unescape(issue.description),
            }
        }

        if issue.priority:
            args["requestFieldValues"]["priority"] = {"name": issue.priority}

        support_customer = issue.caller.supportcustomer
        args["requestParticipants"] = [support_customer.backend_id]

        return args

    def _get_first_sla_field(self, backend_issue):
        field_name = self.get_field_id_by_name(config.ATLASSIAN_SLA_FIELD)
        value = getattr(backend_issue.fields, field_name, None)
        if value and hasattr(value, "ongoingCycle"):
            epoch_milliseconds = value.ongoingCycle.breachTime.epochMillis
            if epoch_milliseconds:
                return datetime.fromtimestamp(
                    epoch_milliseconds / 1000.0, timezone.get_default_timezone()
                )

    def _backend_issue_to_issue(self, backend_issue, issue):
        issue.key = backend_issue.key
        issue.backend_id = backend_issue.key
        issue.resolution = (
            backend_issue.fields.resolution and backend_issue.fields.resolution.name
        ) or ""
        issue.status = backend_issue.fields.status.name or ""
        issue.link = backend_issue.permalink()
        issue.priority = backend_issue.fields.priority.name
        issue.first_response_sla = self._get_first_sla_field(backend_issue)
        issue.summary = backend_issue.fields.summary
        issue.description = backend_issue.fields.description or ""
        issue.type = backend_issue.fields.issuetype.name
        issue.resolution_date = backend_issue.fields.resolutiondate or None
        issue.feedback_request = self.get_request_feedback_field(backend_issue)

        def get_support_user_by_field(fields, field_name):
            backend_user = getattr(fields, field_name, None)

            if backend_user:
                return self.get_or_create_support_user(backend_user)

        impact_field_id = self.get_field_id_by_name(config.ATLASSIAN_IMPACT_FIELD)
        impact = getattr(backend_issue.fields, impact_field_id, None)
        if impact:
            issue.impact = impact

        assignee = get_support_user_by_field(backend_issue.fields, "assignee")
        if assignee:
            issue.assignee = assignee

        reporter = get_support_user_by_field(backend_issue.fields, "reporter")
        if reporter:
            issue.reporter = reporter

    def get_or_create_support_user(self, user):
        user_id = self.get_user_id(user)
        if user_id:
            author, _ = models.SupportUser.objects.get_or_create(backend_id=user_id)
            return author

    def get_user_id(self, user):
        try:
            if self.use_old_api:
                return user.name  # alias for username
            else:
                return user.key
        except AttributeError:
            return user.accountId
        except TypeError:
            return

    def _backend_comment_to_comment(self, backend_comment, comment):
        comment.update_message(backend_comment.body)
        comment.author = self.get_or_create_support_user(backend_comment.author)
        try:
            internal = self._get_property(
                "comment", backend_comment.id, "sd.public.comment"
            )
            comment.is_public = not internal.get("value", {}).get("internal", False)
        except JIRAError:
            # workaround for backbone-issue-sync-for-jira plugin
            try:
                external = self._get_property(
                    "comment", backend_comment.id, "sd.allow.public.comment"
                )
                comment.is_public = external.get("value", {}).get("allow", False)
            except JIRAError:
                comment.is_public = False

    def _backend_attachment_to_attachment(self, backend_attachment, attachment):
        attachment.mime_type = getattr(backend_attachment, "mimeType", "")
        attachment.file_size = backend_attachment.size
        attachment.created = dateutil.parser.parse(backend_attachment.created)
        attachment.author = self.get_or_create_support_user(backend_attachment.author)

    @reraise_exceptions
    def pull_request_types(self):
        service_desk_id = self.manager.waldur_service_desk(config.ATLASSIAN_PROJECT_ID)
        # backend_request_types = self.manager.request_types(service_desk_id)
        backend_request_types = self.manager.waldur_request_types(
            service_desk_id, config.ATLASSIAN_PROJECT_ID, self.strange_setting
        )

        with transaction.atomic():
            backend_request_type_map = {
                int(request_type.id): request_type
                for request_type in backend_request_types
            }

            waldur_request_type = {
                request_type.backend_id: request_type
                for request_type in models.RequestType.objects.all()
            }

            # cleanup request types if automatic request mapping is done
            if self.use_automatic_request_mapping:
                stale_request_types = set(waldur_request_type.keys()) - set(
                    backend_request_type_map.keys()
                )
                models.RequestType.objects.filter(
                    backend_id__in=stale_request_types
                ).delete()

            for backend_request_type in backend_request_types:
                defaults = {
                    "name": backend_request_type.name,
                    "fields": self.manager.waldur_request_type_fields(
                        service_desk_id, backend_request_type.id
                    ),
                }
                if self.use_automatic_request_mapping:
                    issue_type = self.manager.issue_type(
                        backend_request_type.issueTypeId
                    )
                    defaults["issue_type_name"] = issue_type.name

                models.RequestType.objects.update_or_create(
                    backend_id=backend_request_type.id,
                    defaults=defaults,
                )

    @reraise_exceptions
    def pull_priorities(self):
        backend_priorities = self.manager.priorities()
        with transaction.atomic():
            backend_priorities_map = {
                priority.id: priority for priority in backend_priorities
            }

            waldur_priorities = {
                priority.backend_id: priority
                for priority in models.Priority.objects.all()
            }

            stale_priorities = set(waldur_priorities.keys()) - set(
                backend_priorities_map.keys()
            )
            models.Priority.objects.filter(backend_id__in=stale_priorities).delete()

            for priority in backend_priorities:
                models.Priority.objects.update_or_create(
                    backend_id=priority.id,
                    defaults={
                        "name": priority.name,
                        "description": priority.description,
                        "icon_url": priority.iconUrl,
                    },
                )

    @reraise_exceptions
    def create_issue_links(self, issue, linked_issues):
        for linked_issue in linked_issues:
            link_type = config.ATLASSIAN_LINKED_ISSUE_TYPE
            self.manager.create_issue_link(link_type, issue.key, linked_issue.key)

    def create_feedback(self, feedback):
        if feedback.comment:
            support_user, _ = models.SupportUser.objects.get_or_create_from_user(
                feedback.issue.caller
            )
            comment = models.Comment.objects.create(
                issue=feedback.issue,
                description=feedback.comment,
                is_public=False,
                author=support_user,
            )
            self.create_comment(comment)

        if feedback.evaluation:
            field_name = self.get_field_id_by_name(config.ATLASSIAN_SATISFACTION_FIELD)
            backend_issue = self.get_backend_issue(feedback.issue.backend_id)
            kwargs = {field_name: feedback.get_evaluation_display()}
            backend_issue.update(**kwargs)

    def get_request_feedback_field(self, backend_issue):
        try:
            field_name = self.get_field_id_by_name(
                config.ATLASSIAN_REQUEST_FEEDBACK_FIELD
            )
        except JiraBackendError:
            logger.warning("Field request_feedback is not defined in Jira support.")
            return True
        value = getattr(backend_issue.fields, field_name, None)
        # we treat any value we receive from backend as True. Unset / missing value means False.
        return bool(value)

    def pull_support_users(self):
        """
        Pull support users from backend.
        Note that support users are not deleted in JIRA.
        Instead, they are marked as disabled.
        Therefore, Waldur replicates the same behaviour.
        """

        backend_users = self.get_users()

        for backend_user in backend_users:
            user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.backend_id,
                backend_name=self.backend_name,
                defaults={"name": backend_user.name},
            )
            if not created and user.name != backend_user.name:
                user.name = backend_user.name
                user.save()
            if not user.is_active:
                user.is_active = True
                user.save()

        models.SupportUser.objects.filter(backend_name=self.backend_name).exclude(
            backend_id__in=[u.backend_id for u in backend_users]
        ).update(is_active=False)

    def get_issue_details(self):
        return {"type": config.ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE}
