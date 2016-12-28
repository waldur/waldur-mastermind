from __future__ import unicode_literals
import functools
import json

from django.conf import settings
from django.utils import six
from jira import JIRA, JIRAError, Comment
from jira.utils import json_loads

from nodeconductor_assembly_waldur.support import models
from nodeconductor_assembly_waldur.support.backend import SupportBackendError, SupportBackend


class JiraBackendError(SupportBackendError):
    pass


class JiraBackend(SupportBackend):
    credentials = settings.WALDUR_SUPPORT.get('CREDENTIALS', {})
    project_details = settings.WALDUR_SUPPORT.get('PROJECT', {})

    def reraise_exceptions(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except JIRAError as e:
                six.reraise(JiraBackendError, e)
        return wrapped

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
        caller_name = issue.caller.full_name or issue.caller.username
        args = {
            'project': self.project_details['key'],
            'summary': issue.summary,
            'description': issue.description,
            'issuetype': {'name': issue.type},
            self._get_field_id_by_name(self.project_details['caller_field']): caller_name,
        }
        if issue.reporter:
            args[self._get_field_id_by_name(self.project_details['reporter_field'])] = issue.reporter.name
            # args['reporter'] = {'name': issue.reporter.name}
        if issue.impact:
            args[self._get_field_id_by_name(self.project_details['impact_field'])] = issue.impact
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
        issue.save()

    @reraise_exceptions
    def update_issue(self, issue):
        backend_issue = self.manager.issue(issue.backend_id)
        backend_issue.update(summary=issue.summary, description=issue.description)

    @reraise_exceptions
    def delete_issue(self, issue):
        backend_issue = self.manager.issue(issue.backend_id)
        backend_issue.delete()

    def _prepare_comment_message(self, comment):
        return comment.description

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
        users = self.manager.search_assignable_users_for_projects('', self.project_details['key'], maxResults=False)
        return [models.SupportUser(name=user.displayName, backend_id=user.key) for user in users]


class ServiceDeskBackend(JiraBackend):

    def create_comment(self, comment):
        backend_comment = self._add_comment(
            comment.issue.backend_id,
            self._prepare_comment_message(comment),
            is_internal=comment.is_public,
        )
        comment.backend_id = backend_comment.id
        comment.save(update_fields=['backend_id'])

    def _add_comment(self, issue, body, is_internal):
        data = {
            'body': body,
            'properties': [{'key': 'sd.public.comment', 'value': {'internal': is_internal}}, ]
        }

        url = self.manager._get_url('issue/{0}/comment'.format(issue))
        r = self.manager._session.post(
            url, data=json.dumps(data))

        comment = Comment(self._options, self._session, raw=json_loads(r))
        return comment