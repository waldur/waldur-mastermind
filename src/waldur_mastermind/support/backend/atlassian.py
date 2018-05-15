from __future__ import unicode_literals

import collections
import json
import logging
from datetime import datetime

import dateutil.parser
from django.conf import settings
from django.utils import timezone
from jira import Comment
from jira.utils import json_loads
from six.moves.html_parser import HTMLParser

from waldur_jira.backend import reraise_exceptions, JiraBackend
from waldur_mastermind.support import models

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

    def expand_comments(self, issue_key):
        """
        Returns a list of comments expanded with the 'properties' attribute.
        An attribute is taken from JIRA by adding an 'expand' query parameter to the issue GET URL.
        A method is required as Jira does not indicate internal status of comments.
        More info: https://jira.atlassian.com/browse/JSD-1261.
        :param issue_key: issue key to get comments from.
        :return: list of Jira comments as dictionaries.
        """
        url = self.manager._get_url('issue/%s/comment?expand=properties' % issue_key)
        response = self.manager._session.get(url)

        backend_comments = json.loads(response.text)['comments']
        return backend_comments

    @reraise_exceptions
    def create_issue(self, issue):
        if not issue.caller.email:
            return

        self.create_user(self.issue.caller)
        super(ServiceDeskBackend, self).create_issue(issue)

    def create_user(self, user):
        return self.manager.add_user(user.email, user.email, fullname=user.full_name, ignore_existing=True)

    @reraise_exceptions
    def get_users(self):
        users = self.manager.search_assignable_users_for_projects('', self.project_settings['key'], maxResults=False)
        return [models.SupportUser(name=user.displayName, backend_id=user.key) for user in users]

    def _issue_to_dict(self, issue):
        parser = HTMLParser()
        args = {
            'project': self.project_settings['key'],
            'summary': parser.unescape(issue.summary),
            'description': parser.unescape(issue.description),
            'issuetype': {'name': issue.type},
        }

        if issue.reporter:
            args[self.get_field_id_by_name(self.issue_settings['reporter_field'])] = issue.reporter.name
        if issue.impact:
            args[self.get_field_id_by_name(self.issue_settings['impact_field'])] = issue.impact
        if issue.priority:
            args['priority'] = {'name': issue.priority}

        args[self.get_field_id_by_name(self.issue_settings['caller_field'])] = [{
            "name": issue.caller.email,
            "key": issue.caller.email
        }]
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

        def get_support_user_by_field(fields, field_name):
            support_user = None
            backend_user = getattr(fields, field_name, None)

            if backend_user:
                support_user_backend_key = getattr(backend_user, 'key', None)

                if support_user_backend_key:
                    support_user, _ = models.SupportUser.objects.get_or_create(backend_id=support_user_backend_key)

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

    def _backend_comment_to_comment(self, backend_comment, comment):
        comment.update_message(backend_comment.body)
        author, _ = models.SupportUser.objects.get_or_create(backend_id=backend_comment.author.key)
        comment.author = author
        internal = self._get_property('comment', backend_comment.id, 'sd.public.comment')
        comment.is_public = not internal.get('value', {}).get('internal', False)

    def _backend_attachment_to_attachment(self, backend_attachment, attachment):
        author, _ = models.SupportUser.objects.get_or_create(backend_id=backend_attachment.author.key)
        attachment.mime_type = getattr(backend_attachment, 'mimeType', '')
        attachment.file_size = backend_attachment.size
        attachment.created = dateutil.parser.parse(backend_attachment.created)
        attachment.author = author
