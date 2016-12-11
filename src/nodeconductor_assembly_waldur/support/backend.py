from jira import JIRA

from django.conf import settings


def get_active_backned():
    return globals()[settings.WALDUR_SUPPORT['ACTIVE_BACKEND']]()


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


class JIRABackend(SupportBackend):
    credentials = settings.WALDUR_SUPPORT.get('CREDENTIALS', {})
    project_details = settings.WALDUR_SUPPORT.get('PROJECT', {})

    @property
    def manager(self):
        # manager will be the same for all issues - we can cache it on the class level.
        if not hasattr(self.__class__, '_manager'):
            self.__class__._manager = JIRA(
                server=self.credentials['server'],
                options={'verify': self.credentials['verify_ssl']},
                basic_auth=(self.credentials['username'], self.credentials['password']),
                validate=False)
        return self.__class__._manager

    def _get_field_id_by_name(self, field_name):
        if not field_name:
            return None
        if not hasattr(self.__class__, '_fields'):
            self.__class__._fields = self.manager.fields()
        return next(f['id'] for f in self.__class__._fields if field_name in f['clauseNames'])

    def _issue_to_dict(self, issue):
        """ Convert issue to dict that can be accepted by JIRA as input parameters """
        # issue_type = str(dict(models.Issue.Type.CHOICES)[issue.type])
        args = {
            'project': self.project_details['key'],
            'summary': issue.summary,
            'description': issue.description,
            'issuetype': {'name': issue.type},
            self._get_field_id_by_name(self.project_details['reporter_field']): issue.reporter.name,
        }
        if issue.impact:
            args[self._get_field_id_by_name(self.project_details['impact_field'])] = issue.impact
        if issue.priority:
            args['priority'] = {'name': issue.priority}
        return args

    def create_issue(self, issue):
        backend_issue = self.manager.create_issue(**self._issue_to_dict(issue))
        issue.key = backend_issue.key
        issue.backend_id = backend_issue.key
        issue.resolution = backend_issue.fields.resolution or ''
        issue.status = backend_issue.fields.status.name or ''
        issue.link = backend_issue.permalink()
        issue.priority = backend_issue.fields.priority.name
        issue.save()

    def update_issue(self, issue):
        backend_issue = self.manager.issue(issue.backend_id)
        backend_issue.update(summary=issue.summary, description=issue.description)

    def delete_issue(self, issue):
        backend_issue = self.manager.issue(issue.backend_id)
        backend_issue.delete()

    def _prepare_comment_message(self, comment):
        return comment.description

    def create_comment(self, comment):
        backend_comment = self.manager.add_comment(comment.issue.backend_id, self._prepare_comment_message(comment))
        comment.backend_id = backend_comment.id
        comment.save(update_fields=['backend_id'])

    def update_comment(self, comment):
        backend_comment = self.manager.comment(comment.issue.backend_id, comment.backend_id)
        backend_comment.update(body=self._prepare_comment_message(comment))

    def delete_comment(self, comment):
        backend_comment = self.manager.comment(comment.issue.backend_id, comment.backend_id)
        backend_comment.delete()
