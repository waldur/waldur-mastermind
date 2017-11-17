from __future__ import unicode_literals

from datetime import datetime
import functools
import re
from six.moves.html_parser import HTMLParser
import json

from django.conf import settings
from django.utils import six
from jira import JIRA, JIRAError, Comment
from jira.utils import json_loads

from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendError, SupportBackend
from waldur_mastermind.support.log import event_logger


class JiraBackendError(SupportBackendError):
    pass


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except JIRAError as e:
            six.reraise(JiraBackendError, e)

    return wrapped


class JiraBackend(SupportBackend):
    credentials = settings.WALDUR_SUPPORT.get('CREDENTIALS', {})
    project_settings = settings.WALDUR_SUPPORT.get('PROJECT', {})
    issue_settings = settings.WALDUR_SUPPORT.get('ISSUE', {})

    @property
    @reraise_exceptions
    def manager(self):
        # manager will be the same for all issues - we can cache it on the class level.
        if not hasattr(self.__class__, '_manager'):
            self.__class__._manager = JIRA(
                server=self.credentials['server'],
                options={'verify': self.credentials['verify_ssl']},
                basic_auth=(self.credentials['username'], self.credentials['password']),
                validate=False)
        return self.__class__._manager

    @reraise_exceptions
    def _get_field_id_by_name(self, field_name):
        if not hasattr(self.__class__, '_fields'):
            self.__class__._fields = self.manager.fields()
        try:
            return next(f['id'] for f in self.__class__._fields if field_name in f['clauseNames'])
        except StopIteration:
            return JiraBackendError('Field "{0}" does not exist in JIRA.'.format(field_name))

    def _issue_to_dict(self, issue):
        """ Convert issue to dict that can be accepted by JIRA as input parameters """
        caller = issue.caller.full_name or issue.caller.username
        parser = HTMLParser()
        args = {
            'project': self.project_settings['key'],
            'summary': parser.unescape(issue.summary),
            'description': parser.unescape(issue.description),
            'issuetype': {'name': issue.type},
            self._get_field_id_by_name(self.issue_settings['caller_field']): caller,
        }

        if issue.reporter:
            args[self._get_field_id_by_name(self.issue_settings['reporter_field'])] = issue.reporter.name
        if issue.impact:
            args[self._get_field_id_by_name(self.issue_settings['impact_field'])] = issue.impact
        if issue.priority:
            args['priority'] = {'name': issue.priority}
        return args

    @reraise_exceptions
    def create_issue(self, issue):
        backend_issue = self.manager.create_issue(**self._issue_to_dict(issue))
        if issue.assignee:
            self.manager.assign_issue(backend_issue.key, issue.assignee.backend_id)
        issue.key = backend_issue.key
        issue.backend_id = backend_issue.key
        issue.resolution = backend_issue.fields.resolution or ''
        issue.status = backend_issue.fields.status.name or ''
        issue.link = backend_issue.permalink()
        issue.priority = backend_issue.fields.priority.name
        issue.first_response_sla = self._get_first_sla_field(backend_issue)
        issue.save()

        event_logger.waldur_issue.info(
            'Issue {issue_key} has been created.',
            event_type='issue_creation_succeeded',
            event_context={
                'issue': issue,
            })

        return backend_issue

    def _get_first_sla_field(self, backend_issue):
        field_name = self._get_field_id_by_name(self.issue_settings['sla_field'])
        value = getattr(backend_issue.fields, field_name, None)
        if value and hasattr(value, 'ongoingCycle'):
            epoch_milliseconds = value.ongoingCycle.breachTime.epochMillis
            if epoch_milliseconds:
                return datetime.fromtimestamp(epoch_milliseconds / 1000.0)

    @reraise_exceptions
    def update_issue(self, issue):
        backend_issue = self.manager.issue(issue.backend_id)
        backend_issue.update(summary=issue.summary, description=issue.description)

    @reraise_exceptions
    def delete_issue(self, issue):
        backend_issue = self.manager.issue(issue.backend_id)
        backend_issue.delete()

    def _prepare_comment_message(self, comment):
        """
        Prepends user info to the comment description to display comment author in JIRA.
        User info format - '[user.full_name user.civil_number]: '.
        """
        return '[%s %s]: %s' % (comment.author.user.full_name or comment.author.user.username,
                                comment.author.user.civil_number or '',
                                comment.description)

    def extract_comment_message(self, comment_body):
        """
        Extracts comment message from JIRA comment which contains user's info in its body.
        """
        match = re.search('^(\[.*?\]\:\s)', comment_body)
        return comment_body.replace(match.group(0), '') if match else comment_body

    @reraise_exceptions
    def create_comment(self, comment):
        backend_comment = self.manager.add_comment(comment.issue.backend_id, self._prepare_comment_message(comment))
        comment.backend_id = backend_comment.id
        comment.save(update_fields=['backend_id'])

    @reraise_exceptions
    def update_comment(self, comment):
        backend_comment = self.manager.comment(comment.issue.backend_id, comment.backend_id)
        backend_comment.update(body=self._prepare_comment_message(comment))

    @reraise_exceptions
    def delete_comment(self, comment):
        backend_comment = self.manager.comment(comment.issue.backend_id, comment.backend_id)
        backend_comment.delete()

    @reraise_exceptions
    def get_users(self):
        users = self.manager.search_assignable_users_for_projects('', self.project_settings['key'], maxResults=False)
        return [models.SupportUser(name=user.displayName, backend_id=user.key) for user in users]


class ServiceDeskBackend(JiraBackend):
    servicedeskapi_path = 'servicedeskapi'

    @reraise_exceptions
    def create_comment(self, comment):
        backend_comment = self._add_comment(
            comment.issue.backend_id,
            self._prepare_comment_message(comment),
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

        # customer will be associated with the issue by updating issue arguments in _issue_to_dict.
        self._create_customer(issue.caller.email, issue.caller.full_name)
        super(ServiceDeskBackend, self).create_issue(issue)

    def _issue_to_dict(self, issue):
        args = super(ServiceDeskBackend, self)._issue_to_dict(issue)
        args[self._get_field_id_by_name(self.issue_settings['caller_field'])] = [{
            "name": issue.caller.email,
            "key": issue.caller.email
        }]
        return args

    def _create_customer(self, email, full_name):
        """
        Creates customer in Jira Service Desk without assigning it to any particular service desk.
        :param email: customer email
        :param full_name: customer full name
        :return: True if customer is created. False if user exists already.
        """
        data = {
            "fullName": full_name,
            "email": email
        }

        headers = {
            'X-ExperimentalApi': 'true',
        }

        url = "{host}rest/{path}/customer".format(host=self.credentials['server'], path=self.servicedeskapi_path)
        try:
            self.manager._session.post(url, data=json.dumps(data), headers=headers)
        except JIRAError as e:
            # TODO [TM:1/11/17] replace it with api call when such an ability is provided
            if e.status_code == 400 and "already exists" in e.text:
                return False
            else:
                raise e
        else:
            return True
