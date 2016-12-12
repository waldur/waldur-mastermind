import functools

from django.utils import six
from jira import JIRA, JIRAError

from django.conf import settings


def get_active_backend():
    return globals()[settings.WALDUR_SUPPORT['ACTIVE_BACKEND']]()


class SupportBackendError(Exception):
    pass


class SupportBackend(object):
    """ Interface for support backend """
    def create_issue(self, issue):
        pass

    def update_issue(self, issue):
        pass

    def delete_issue(self, issue):
        pass

    def create_comment(self, comment):
        pass

    def update_comment(self, comment):
        pass

    def delete_comment(self, comment):
        pass


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
            return JiraBackendError('Field "%s" does not exist in JIRA.' % field_name)

    def _issue_to_dict(self, issue):
        """ Convert issue to dict that can be accepted by JIRA as input parameters """
        args = {
            'project': self.project_details['key'],
            'summary': issue.summary,
            'description': issue.description,
            'issuetype': {'name': issue.type},
            self._get_field_id_by_name(self.project_details['reporter_field']): issue.reporter.name,
            self._get_field_id_by_name(self.project_details['caller_field']): issue.caller.name,
        }
        if issue.impact:
            args[self._get_field_id_by_name(self.project_details['impact_field'])] = issue.impact
        if issue.priority:
            args['priority'] = {'name': issue.priority}
        return args

    @reraise_exceptions
    def create_issue(self, issue):
        backend_issue = self.manager.create_issue(**self._issue_to_dict(issue))
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
