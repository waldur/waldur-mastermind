from __future__ import unicode_literals

import collections
import json
import logging
from datetime import datetime

import dateutil.parser
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from jira import Comment
from jira.utils import json_loads
from six.moves.html_parser import HTMLParser

from waldur_jira.backend import reraise_exceptions, JiraBackend
from waldur_mastermind.support import models
from waldur_mastermind.support.exceptions import SupportUserInactive

from . import SupportBackend

logger = logging.getLogger(__name__)


Settings = collections.namedtuple('Settings', ['backend_url', 'username', 'password'])


class ServiceDeskBackend(JiraBackend, SupportBackend):
    servicedeskapi_path = 'servicedeskapi'
    model_comment = models.Comment
    model_issue = models.Issue
    model_attachment = models.Attachment

    def __init__(self):
        self.settings = Settings(
            backend_url=settings.WALDUR_SUPPORT.get('CREDENTIALS', {}).get('server'),
            username=settings.WALDUR_SUPPORT.get('CREDENTIALS', {}).get('username'),
            password=settings.WALDUR_SUPPORT.get('CREDENTIALS', {}).get('password'),
        )
        self.verify = settings.WALDUR_SUPPORT.get('CREDENTIALS', {}).get('verify_ssl')
        self.project_settings = settings.WALDUR_SUPPORT.get('PROJECT', {})
        self.issue_settings = settings.WALDUR_SUPPORT.get('ISSUE', {})
        self.use_old_api = settings.WALDUR_SUPPORT.get('USE_OLD_API', False)

    def pull_service_properties(self):
        super(ServiceDeskBackend, self).pull_service_properties()
        self.pull_request_types()

    @reraise_exceptions
    def create_comment(self, comment):
        backend_comment = self._add_comment(
            comment.issue.backend_id,
            comment.prepare_message(),
            is_internal=not comment.is_public,
        )
        comment.backend_id = backend_comment.id
        comment.save(update_fields=['backend_id'])

    def _add_comment(self, issue, body, is_internal):
        data = {
            'body': body,
            'properties': [{'key': 'sd.public.comment', 'value': {'internal': is_internal}}, ]
        }

        url = self.manager._get_url('issue/{0}/comment'.format(issue))
        response = self.manager._session.post(url, data=json.dumps(data))

        comment = Comment(self.manager._options, self.manager._session, raw=json_loads(response))
        return comment

    @reraise_exceptions
    def create_issue(self, issue):
        if not issue.caller.email:
            return

        self.create_user(issue.caller)

        if self.use_old_api:
            return super(ServiceDeskBackend, self).create_issue(issue)

        args = self._issue_to_dict(issue)
        args['serviceDeskId'] = self.manager.service_desk(self.project_settings['key'])
        if not models.RequestType.objects.filter(issue_type_name=issue.type).count():
            self.pull_request_types()

        if not models.RequestType.objects.filter(issue_type_name=issue.type).count():
            logger.debug('Not exists a RequestType for this issue type %s', issue.type)
            return

        args['requestTypeId'] = models.RequestType.objects.filter(issue_type_name=issue.type).first().backend_id
        backend_issue = self.manager.create_customer_request(args)
        args = self._get_custom_fields(issue)

        # Update an issue, because create_customer_request doesn't allow setting custom fields.
        backend_issue.update(**args)
        self._backend_issue_to_issue(backend_issue, issue)
        issue.save()

    def create_user(self, user):
        # Temporary workaround as JIRA returns 500 error if user already exists
        if self.use_old_api:
            # old API has a bug that causes user active status to be set to False if includeInactive is passed as True
            existing_support_user = self.manager.search_users(user.email)
        else:
            existing_support_user = self.manager.search_users(user.email, includeInactive=True)

        if existing_support_user:
            active_user = [u for u in existing_support_user if u.active]
            if not active_user:
                raise SupportUserInactive()

            logger.debug('Skipping user %s creation because it already exists', user.email)
            backend_customer = active_user[0]
        else:
            if self.use_old_api:
                # add_user method returns boolean value therefore we need to fetch user object to find its key
                self.manager.add_user(user.email, user.email, fullname=user.full_name, ignore_existing=True)
                backend_customer = self.manager.search_users(user.email)[0]
            else:
                backend_customer = self.manager.create_customer(user.email, user.full_name)

        try:
            user.supportcustomer
        except ObjectDoesNotExist:
            support_customer = models.SupportCustomer(user=user, backend_id=backend_customer.key)
            support_customer.save()

    @reraise_exceptions
    def get_users(self):
        users = self.manager.search_assignable_users_for_projects('', self.project_settings['key'], maxResults=False)
        return [models.SupportUser(name=user.displayName, backend_id=user.key) for user in users]

    def _get_custom_fields(self, issue):
        args = {}

        if issue.reporter:
            args[self.get_field_id_by_name(self.issue_settings['reporter_field'])] = issue.reporter.name
        if issue.impact:
            args[self.get_field_id_by_name(self.issue_settings['impact_field'])] = issue.impact
        if issue.priority:
            args['priority'] = {'name': issue.priority}

        def set_custom_field(field_name, value):
            if value and self.issue_settings.get(field_name):
                args[self.get_field_id_by_name(self.issue_settings[field_name])] = value

        if issue.reporter and issue.reporter.user and issue.reporter.user.organization:
            set_custom_field('organisation_field', issue.reporter.user.organization)

        if issue.project:
            set_custom_field('project_field', issue.project.name)

        if issue.resource:
            set_custom_field('affected_resource_field', issue.resource)

        if issue.template:
            set_custom_field('template_field', issue.template.name)

        return args

    def _issue_to_dict(self, issue):
        parser = HTMLParser()

        if self.use_old_api:
            parser = HTMLParser()
            args = {
                'project': self.project_settings['key'],
                'summary': parser.unescape(issue.summary),
                'description': parser.unescape(issue.description),
                'issuetype': {'name': issue.type},
            }
            args.update(self._get_custom_fields(issue))

            try:
                support_user = models.SupportUser.objects.get(user=issue.caller)
                key = support_user.backend_id or issue.caller.email
            except models.SupportUser.DoesNotExist:
                key = issue.caller.email

            args[self.get_field_id_by_name(self.issue_settings['caller_field'])] = [{
                "name": key,  # will be equal to issue.caller.email for non-support users
                "key": key,
            }]
            return args

        args = {
            'requestFieldValues': {
                'summary': parser.unescape(issue.summary),
                'description': parser.unescape(issue.description)
            }
        }

        support_customer = issue.caller.supportcustomer
        args['requestParticipants'] = [support_customer.backend_id]
        return args

    def _get_first_sla_field(self, backend_issue):
        field_name = self.get_field_id_by_name(self.issue_settings['sla_field'])
        value = getattr(backend_issue.fields, field_name, None)
        if value and hasattr(value, 'ongoingCycle'):
            epoch_milliseconds = value.ongoingCycle.breachTime.epochMillis
            if epoch_milliseconds:
                return datetime.fromtimestamp(epoch_milliseconds / 1000.0, timezone.get_default_timezone())

    def _backend_issue_to_issue(self, backend_issue, issue):
        issue.key = backend_issue.key
        issue.backend_id = backend_issue.key
        issue.resolution = backend_issue.fields.resolution or ''
        issue.status = backend_issue.fields.status.name or ''
        issue.link = backend_issue.permalink()
        issue.priority = backend_issue.fields.priority.name
        issue.first_response_sla = self._get_first_sla_field(backend_issue)
        issue.summary = backend_issue.fields.summary
        issue.description = backend_issue.fields.description or ''
        issue.type = backend_issue.fields.issuetype.name
        issue.resolution_date = backend_issue.fields.resolutiondate or None

        def get_support_user_by_field(fields, field_name):
            support_user = None
            backend_user = getattr(fields, field_name, None)

            if backend_user:
                try:
                    support_user_backend_key = getattr(backend_user, 'key', None)
                    if support_user_backend_key:
                        support_user, _ = models.SupportUser.objects.get_or_create(backend_id=support_user_backend_key)

                except TypeError:
                    # except TypeError because 'item in self.raw' in here jira/resources.py:173
                    pass

            return support_user

        impact_field_id = self.get_field_id_by_name(self.issue_settings['impact_field'])
        impact = getattr(backend_issue.fields, impact_field_id, None)
        if impact:
            issue.impact = impact

        assignee = get_support_user_by_field(backend_issue.fields, 'assignee')
        if assignee:
            issue.assignee = assignee

        reporter = get_support_user_by_field(backend_issue.fields, 'reporter')
        if reporter:
            issue.reporter = reporter

    def _get_author(self, resource):
        backend_id = resource.raw.get('author', {}).get('key')
        author, _ = models.SupportUser.objects.get_or_create(backend_id=backend_id)
        return author

    def _backend_comment_to_comment(self, backend_comment, comment):
        comment.update_message(backend_comment.body)
        author = self._get_author(backend_comment)
        comment.author = author
        internal = self._get_property('comment', backend_comment.id, 'sd.public.comment')
        comment.is_public = not internal.get('value', {}).get('internal', False)

    def _backend_attachment_to_attachment(self, backend_attachment, attachment):
        author = self._get_author(backend_attachment)
        attachment.mime_type = getattr(backend_attachment, 'mimeType', '')
        attachment.file_size = backend_attachment.size
        attachment.created = dateutil.parser.parse(backend_attachment.created)
        attachment.author = author

    @reraise_exceptions
    def pull_request_types(self):
        service_desk_id = self.manager.service_desk(self.project_settings['key'])
        backend_request_types = self.manager.request_types(service_desk_id)
        with transaction.atomic():
            backend_request_type_map = {
                int(request_type.id): request_type for request_type in backend_request_types
            }

            waldur_request_type = {
                request_type.backend_id: request_type
                for request_type in models.RequestType.objects.all()
            }

            stale_request_types = set(waldur_request_type.keys()) - set(backend_request_type_map.keys())
            models.RequestType.objects.filter(backend_id__in=stale_request_types).delete()

            for backend_request_type in backend_request_types:
                issue_type = self.manager.issue_type(backend_request_type.issueTypeId)
                models.RequestType.objects.update_or_create(
                    backend_id=backend_request_type.id,
                    defaults={
                        'name': backend_request_type.name,
                        'issue_type_name': issue_type.name
                    })
