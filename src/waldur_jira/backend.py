from __future__ import unicode_literals, division

import functools
import logging
import sys

from django.conf import settings
from django.db import transaction, IntegrityError
from django.utils import six
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.functional import cached_property
from jira.client import _get_template_list
from jira.utils import json_loads
from rest_framework import status

from waldur_core.core.models import StateMixin
from waldur_core.structure import ServiceBackend, ServiceBackendError
from waldur_core.structure.utils import update_pulled_fields

from .jira_fix import JIRA, JIRAError
from . import models

logger = logging.getLogger(__name__)


class JiraBackendError(ServiceBackendError):
    pass


def check_captcha(e):
    if e.response is None:
        return False
    if not hasattr(e.response, 'headers'):
        return False
    if 'X-Seraph-LoginReason' not in e.response.headers:
        return False
    return e.response.headers['X-Seraph-LoginReason'] == 'AUTHENTICATED_FAILED'


def reraise(exc):
    """
    Reraise JiraBackendError while maintaining traceback.
    """
    six.reraise(JiraBackendError, exc, sys.exc_info()[2])


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except JIRAError as e:
            reraise(e)

    return wrapped


class JiraBackend(ServiceBackend):
    """ Waldur interface to JIRA.
        http://pythonhosted.org/jira/
        http://docs.atlassian.com/jira/REST/latest/
    This class can be overridden in other modules that use JIra.
    Declare used models of other modules:
        - model_comment
        - model_issue
        - model_attachment
    And if necessary, overridden methods
        - _backend_issue_to_issue
        - _backend_comment_to_comment
        - _backend_attachment_to_attachment

    """

    model_comment = models.Comment
    model_issue = models.Issue
    model_attachment = models.Attachment
    model_project = models.Project

    def __init__(self, settings, project=None, verify=False):
        self.settings = settings
        self.project = project
        self.verify = verify

    def sync(self):
        self.ping(raise_exception=True)
        self.pull_project_templates()
        self.pull_priorities()

    def ping(self, raise_exception=False):
        try:
            self.manager.myself()
        except JIRAError as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    @reraise_exceptions
    def get_resources_for_import(self):
        return [{
            'name': proj.name,
            'backend_id': proj.key,
        } for proj in self.manager.projects()]

    @staticmethod
    def convert_field(value, choices, mapping=None):
        """ Reverse mapping for choice fields """
        if mapping:
            mapping = {v: k for k, v in mapping.items()}
            value = mapping.get(value, value)

        try:
            return next(k for k, v in choices if v == value)
        except StopIteration:
            return 0

    @property
    def manager(self):
        try:
            return getattr(self, '_manager')
        except AttributeError:
            try:
                self._manager = JIRA(
                    server=self.settings.backend_url,
                    options={'verify': self.verify},
                    basic_auth=(self.settings.username, self.settings.password),
                    validate=False)
            except JIRAError as e:
                if check_captcha(e):
                    raise JiraBackendError('JIRA CAPTCHA is triggered. Please reset credentials.')
                reraise(e)

            return self._manager

    @reraise_exceptions
    def get_field_id_by_name(self, field_name):
        if not field_name:
            return None
        try:
            fields = getattr(self, '_fields')
        except AttributeError:
            fields = self._fields = self.manager.fields()
        try:
            return next(f['id'] for f in fields if field_name in f['clauseNames'])
        except StopIteration:
            raise JiraBackendError("Can't find custom field %s" % field_name)

    @reraise_exceptions
    def get_project_templates(self):
        url = self.manager._options['server'] + '/rest/project-templates/latest/templates'

        response = self.manager._session.get(url)
        json_data = json_loads(response)
        return _get_template_list(json_data)

    @reraise_exceptions
    def pull_project_templates(self):
        backend_templates = self.get_project_templates()
        with transaction.atomic():
            for template in backend_templates:
                backend_id = template['projectTemplateModuleCompleteKey']
                icon_url = self.manager._options['server'] + template['iconUrl']
                models.ProjectTemplate.objects.update_or_create(
                    backend_id=backend_id,
                    defaults={
                        'name': template['name'],
                        'description': template['description'],
                        'icon_url': icon_url,
                    })

    @reraise_exceptions
    def pull_priorities(self):
        backend_priorities = self.manager.priorities()
        with transaction.atomic():
            backend_priorities_map = {
                priority.id: priority for priority in backend_priorities
            }

            waldur_priorities = {
                priority.backend_id: priority
                for priority in models.Priority.objects.filter(settings=self.settings)
            }

            stale_priorities = set(waldur_priorities.keys()) - set(backend_priorities_map.keys())
            models.Priority.objects.filter(backend_id__in=stale_priorities)

            for priority in backend_priorities:
                models.Priority.objects.update_or_create(
                    backend_id=priority.id,
                    settings=self.settings,
                    defaults={
                        'name': priority.name,
                        'description': priority.description,
                        'icon_url': priority.iconUrl,
                    })

    @reraise_exceptions
    def import_priority(self, priority):
        return models.Priority(
            backend_id=priority.id,
            settings=self.settings,
            name=priority.name,
            description=getattr(property, 'description', ''),
            icon_url=priority.iconUrl,
        )

    @reraise_exceptions
    def get_project(self, project_id):
        return self.manager.project(project_id)

    @cached_property
    def default_assignee(self):
        # JIRA REST API basic authentication accepts either username or email.
        # But create project endpoint does not accept email.
        # Therefore we need to get username for the logged in user.
        user = self.manager.myself()
        return user['name']

    @reraise_exceptions
    def create_project(self, project):
        self.manager.create_project(
            key=project.backend_id,
            name=project.name,
            assignee=self.default_assignee,
            template_name=project.template.name,
        )
        self.pull_issue_types(project)

    def pull_issue_types(self, project):
        backend_project = self.get_project(project.backend_id)
        backend_issue_types = {
            issue_type.id: issue_type
            for issue_type in backend_project.issueTypes
        }
        project_issue_types = {
            issue_type.backend_id: issue_type
            for issue_type in project.issue_types.all()
        }
        global_issue_types = {
            issue_type.backend_id: issue_type
            for issue_type in models.IssueType.objects.filter(settings=self.settings)
        }

        new_issue_types = set(backend_issue_types.keys()) - set(project_issue_types.keys())
        for issue_type_id in new_issue_types:
            if issue_type_id in global_issue_types:
                issue_type = global_issue_types[issue_type_id]
            else:
                issue_type = self.import_issue_type(backend_issue_types[issue_type_id])
                issue_type.save()
            project.issue_types.add(issue_type)

        stale_issue_types = set(project_issue_types.keys()) - set(backend_issue_types.keys())
        project.issue_types.filter(backend_id__in=stale_issue_types).delete()

        common_issue_types = set(project_issue_types.keys()) & set(backend_issue_types.keys())
        for issue_type_id in common_issue_types:
            issue_type = project_issue_types[issue_type_id]
            imported_issue_type = self.import_issue_type(backend_issue_types[issue_type_id])
            update_pulled_fields(issue_type, imported_issue_type, (
                'name', 'description', 'icon_url', 'subtask'
            ))

    def import_issue_type(self, backend_issue_type):
        return models.IssueType(
            settings=self.settings,
            backend_id=backend_issue_type.id,
            name=backend_issue_type.name,
            description=backend_issue_type.description,
            icon_url=backend_issue_type.iconUrl,
            subtask=backend_issue_type.subtask,
        )

    @reraise_exceptions
    def update_project(self, project):
        backend_project = self.manager.project(project.backend_id)
        backend_project.update(name=project.name)

    @reraise_exceptions
    def delete_project(self, project):
        self.manager.delete_project(project.backend_id)

    @reraise_exceptions
    def create_issue(self, issue):
        args = self._issue_to_dict(issue)
        backend_issue = self.manager.create_issue(**args)
        self._backend_issue_to_issue(backend_issue, issue)
        issue.save()

    def create_issue_from_jira(self, project, key):
        backend_issue = self.get_backend_issue(key)
        if not backend_issue:
            logger.debug('Unable to create issue with key=%s, '
                         'because it has already been deleted on backend.', key)
            return

        issue = self.model_issue(project=project, backend_id=key, state=StateMixin.States.OK)
        self._backend_issue_to_issue(backend_issue, issue)
        try:
            issue.save()
        except IntegrityError:
            logger.debug('Unable to create issue with key=%s, '
                         'because it has been created in another thread.', key)

    def update_issue(self, issue):
        backend_issue = self.get_backend_issue(issue.backend_id)
        if not backend_issue:
            logger.debug('Unable to update issue with key=%s, '
                         'because it has already been deleted on backend.', issue.backend_id)
            return

        backend_issue.update(summary=issue.summary, description=issue.get_description())

    def update_issue_from_jira(self, issue):
        start_time = timezone.now()

        backend_issue = self.get_backend_issue(issue.backend_id)
        if not backend_issue:
            logger.debug('Unable to update issue with key=%s, '
                         'because it has already been deleted on backend.', issue.backend_id)
            return

        issue.refresh_from_db()

        if issue.modified > start_time:
            logger.debug('Skipping issue update with key=%s, '
                         'because it has been updated from other thread.', issue.backend_id)
            return

        self._backend_issue_to_issue(backend_issue, issue)
        issue.save()

    def delete_issue(self, issue):
        backend_issue = self.get_backend_issue(issue.backend_id)
        if backend_issue:
            backend_issue.delete()
        else:
            logger.debug('Unable to delete issue with key=%s, '
                         'because it has already been deleted on backend.', issue.backend_id)

    def delete_issue_from_jira(self, issue):
        backend_issue = self.get_backend_issue(issue.backend_id)
        if not backend_issue:
            issue.delete()
        else:
            logger.debug('Skipping issue deletion with key=%s, '
                         'because it still exists on backend.', issue.backend_id)

    @reraise_exceptions
    def create_comment(self, comment):
        backend_comment = self.manager.add_comment(comment.issue.backend_id, comment.prepare_message())
        comment.backend_id = backend_comment.id
        comment.save(update_fields=['backend_id'])

    def create_comment_from_jira(self, issue, comment_backend_id):
        backend_comment = self.get_backend_comment(issue.backend_id, comment_backend_id)
        if not backend_comment:
            logger.debug('Unable to create comment with id=%s, '
                         'because it has already been deleted on backend.', comment_backend_id)
            return

        comment = self.model_comment(issue=issue, backend_id=comment_backend_id, state=StateMixin.States.OK)
        self._backend_comment_to_comment(backend_comment, comment)

        try:
            comment.save()
        except IntegrityError:
            logger.debug('Unable to create comment issue_id=%s, backend_id=%s, '
                         'because it already exists  n Waldur.', issue.id, comment_backend_id)

    def update_comment(self, comment):
        backend_comment = self.get_backend_comment(comment.issue.backend_id, comment.backend_id)
        if not backend_comment:
            logger.debug('Unable to update comment with id=%s, '
                         'because it has already been deleted on backend.', comment.id)
            return

        backend_comment.update(body=comment.prepare_message())

    def update_comment_from_jira(self, comment):
        backend_comment = self.get_backend_comment(comment.issue.backend_id, comment.backend_id)
        if not backend_comment:
            logger.debug('Unable to update comment with id=%s, '
                         'because it has already been deleted on backend.', comment.id)
            return

        comment.state = StateMixin.States.OK
        self._backend_comment_to_comment(backend_comment, comment)
        comment.save()

    @reraise_exceptions
    def delete_comment(self, comment):
        backend_comment = self.get_backend_comment(comment.issue.backend_id, comment.backend_id)
        if backend_comment:
            backend_comment.delete()
        else:
            logger.debug('Unable to delete comment with id=%s, '
                         'because it has already been deleted on backend.', comment.id)

    def delete_comment_from_jira(self, comment):
        backend_comment = self.get_backend_comment(comment.issue.backend_id, comment.backend_id)
        if not backend_comment:
            comment.delete()
        else:
            logger.debug('Skipping comment deletion with id=%s, '
                         'because it still exists on backend.', comment.id)

    @reraise_exceptions
    def create_attachment(self, attachment):
        backend_issue = self.get_backend_issue(attachment.issue.backend_id)
        if not backend_issue:
            logger.debug('Unable to add attachment to issue with id=%s, '
                         'because it has already been deleted on backend.', attachment.issue.id)
            return

        backend_attachment = self.manager.waldur_add_attachment(backend_issue, attachment.file.path)
        attachment.backend_id = backend_attachment.id
        attachment.save(update_fields=['backend_id'])

    @reraise_exceptions
    def delete_attachment(self, attachment):
        backend_attachment = self.get_backend_attachment(attachment.backend_id)
        if backend_attachment:
            backend_attachment.delete()
        else:
            logger.debug('Unable to remove attachment with id=%s, '
                         'because it has already been deleted on backend.', attachment.id)

    @reraise_exceptions
    def import_project_issues(self, project, start_at=0, max_results=50, order=None):
        waldur_issues = list(self.model_issue.objects.filter(project=project, backend_id__isnull=False).
                             values_list('backend_id', flat=True))

        jql = 'project=%s' % project.backend_id
        if order:
            jql += ' ORDER BY %s' % order

        for backend_issue in self.manager.search_issues(jql, startAt=start_at, maxResults=max_results, fields='*all'):
            key = backend_issue.key
            if key in waldur_issues:
                logger.debug('Skipping import of issue with key=%s, '
                             'because it already exists in Waldur.', key)
                continue

            issue = self.model_issue(project=project, backend_id=key, state=StateMixin.States.OK)
            self._backend_issue_to_issue(backend_issue, issue)
            issue.save()

            attachment_synchronizer = AttachmentSynchronizer(self, issue, backend_issue)
            attachment_synchronizer.perform_update()

            for backend_comment in backend_issue.fields.comment.comments:
                tmp = issue.comments.model()
                tmp.clean_message(backend_comment.body)
                issue.comments.create(
                    user=tmp.user,
                    message=tmp.message,
                    created=parse_datetime(backend_comment.created),
                    backend_id=backend_comment.id,
                    state=issue.comments.model.States.OK)

    def _import_project(self, project_backend_id, service_project_link, state):
        backend_project = self.get_project(project_backend_id)
        project = self.model_project(
            service_project_link=service_project_link,
            backend_id=project_backend_id,
            state=state)
        self._backend_project_to_project(backend_project, project)
        project.save()
        return project

    def import_project(self, project_backend_id, service_project_link):
        project = self._import_project(project_backend_id, service_project_link,
                                       models.Project.States.OK)
        self.import_project_issues(project)
        return project

    def import_project_scheduled(self, project_backend_id, service_project_link):
        project = self._import_project(project_backend_id, service_project_link,
                                       models.Project.States.OK)
        return project

    def import_project_batch(self, project):
        max_results = settings.WALDUR_JIRA.get('ISSUE_IMPORT_LIMIT')
        start_at = project.action_details.get('current_issue', 0)
        self.import_project_issues(project, order='id', start_at=start_at, max_results=max_results)

        total_added = project.action_details.get('current_issue', 0) + max_results

        if total_added >= project.action_details.get('issues_count', 0):
            project.action_details['current_issue'] = project.action_details.get('issues_count', 0)
            project.action_details['percentage'] = 100
            project.runtime_state = 'success'
        else:
            project.action_details['current_issue'] = total_added
            project.action_details['percentage'] = int((project.action_details['current_issue'] /
                                                        project.action_details['issues_count']) * 100)

        project.save()
        return max_results

    def get_backend_comment(self, issue_backend_id, comment_backend_id):
        return self._get_backend_obj('comment')(issue_backend_id, comment_backend_id)

    def get_backend_issue(self, issue_backend_id):
        return self._get_backend_obj('issue')(issue_backend_id)

    def get_backend_attachment(self, attachment_backend_id):
        return self._get_backend_obj('attachment')(attachment_backend_id)

    def update_attachment_from_jira(self, issue):
        backend_issue = self.get_backend_issue(issue.backend_id)
        AttachmentSynchronizer(self, issue, backend_issue).perform_update()

    def delete_old_comments(self, issue):
        backend_issue = self.get_backend_issue(issue.backend_id)
        CommentSynchronizer(self, issue, backend_issue).perform_update()

    @reraise_exceptions
    def _get_backend_obj(self, method):
        def f(*args, **kwargs):
            try:
                func = getattr(self.manager, method)
                backend_obj = func(*args, **kwargs)
            except JIRAError as e:
                if e.status_code == status.HTTP_404_NOT_FOUND:
                    logger.debug('Jira object {} has been already deleted on backend'.format(method))
                    return
                else:
                    raise e
            return backend_obj
        return f

    def _backend_issue_to_issue(self, backend_issue, issue):
        priority = self._get_or_create_priority(issue.project, backend_issue.fields.priority)
        issue_type = self._get_or_create_issue_type(issue.project, backend_issue.fields.issuetype)
        resolution_sla = self._get_resolution_sla(backend_issue)

        for obj in ['assignee', 'creator', 'reporter']:
            backend_obj = getattr(backend_issue.fields, obj, None)
            fields = [
                ['name', 'displayName'],
                ['username', 'name'],
                ['email', 'emailAddress'],
            ]

            for waldur_key, backend_key in fields:
                value = getattr(backend_obj, backend_key, '')
                setattr(issue, obj + '_' + waldur_key, value)

        issue.priority = priority
        issue.summary = backend_issue.fields.summary
        issue.description = backend_issue.fields.description or ''
        issue.type = issue_type
        issue.status = backend_issue.fields.status.name or ''
        issue.resolution = (backend_issue.fields.resolution and backend_issue.fields.resolution.name) or ''
        issue.resolution_date = backend_issue.fields.resolutiondate
        issue.resolution_sla = resolution_sla
        issue.backend_id = backend_issue.key

    def _backend_comment_to_comment(self, backend_comment, comment):
        comment.update_message(backend_comment.body)

    def _backend_attachment_to_attachment(self, backend_attachment, attachment):
        pass

    def _backend_project_to_project(self, backend_project, project):
        project.name = backend_project.name
        project.description = backend_project.description

    def _get_or_create_priority(self, project, backend_priority):
        try:
            priority = models.Priority.objects.get(
                settings=project.service_project_link.service.settings,
                backend_id=backend_priority.id
            )
        except models.Priority.DoesNotExist:
            priority = self.import_priority(backend_priority)
            priority.save()
        return priority

    def _get_or_create_issue_type(self, project, backend_issue_type):
        try:
            issue_type = models.IssueType.objects.get(
                settings=project.service_project_link.service.settings,
                backend_id=backend_issue_type.id
            )
        except models.IssueType.DoesNotExist:
            issue_type = self.import_issue_type(backend_issue_type)
            issue_type.save()
            project.issue_types.add(issue_type)
        return issue_type

    def _get_resolution_sla(self, backend_issue):
        issue_settings = settings.WALDUR_JIRA.get('ISSUE')
        field_name = self.get_field_id_by_name(issue_settings['resolution_sla_field'])
        value = getattr(backend_issue.fields, field_name, None)

        if value and hasattr(value, 'ongoingCycle'):
            milliseconds = value.ongoingCycle.remainingTime.millis
            if milliseconds:
                resolution_sla = milliseconds / 1000
        else:
            resolution_sla = None
        return resolution_sla

    def _issue_to_dict(self, issue):
        args = dict(
            project=issue.project.backend_id,
            summary=issue.summary,
            description=issue.get_description(),
            issuetype={'name': issue.type.name},
        )

        if issue.priority:
            args['priority'] = {'name': issue.priority.name}

        if issue.parent:
            args['parent'] = {'key': issue.parent.backend_id}

        return args

    def _get_property(self, object_name, object_id, property_name):
        url = self.manager._get_url('{0}/{1}/properties/{2}'.format(object_name, object_id, property_name))
        response = self.manager._session.get(url)
        return response.json()

    def get_issues_count(self, project_key):
        base = '{server}/rest/{rest_path}/{rest_api_version}/{path}'
        page_params = {'jql': 'project=%s' % project_key,
                       'validateQuery': True,
                       'startAt': 0,
                       'fields': [],
                       'maxResults': 0,
                       'expand': None}
        result = self.manager._get_json('search', params=page_params, base=base)
        return result['total']


class AttachmentSynchronizer(object):
    def __init__(self, backend, current_issue, backend_issue):
        self.backend = backend
        self.current_issue = current_issue
        self.backend_issue = backend_issue

    def perform_update(self):
        if self.stale_attachment_ids:
            self.backend.model_attachment.objects.filter(backend_id__in=self.stale_attachment_ids).delete()

        for attachment_id in self.new_attachment_ids:
            self._add_attachment(
                self.current_issue,
                self.get_backend_attachment(attachment_id)
            )

        for attachment_id in self.updated_attachments_ids:
            self._update_attachment(
                self.current_issue,
                self.get_backend_attachment(attachment_id),
                self.get_current_attachment(attachment_id)
            )

    def get_current_attachment(self, attachment_id):
        return self.current_attachments_map[attachment_id]

    def get_backend_attachment(self, attachment_id):
        return self.backend_attachments_map[attachment_id]

    @cached_property
    def current_attachments_map(self):
        return {
            six.text_type(attachment.backend_id): attachment
            for attachment in self.current_issue.attachments.all()
        }

    @cached_property
    def current_attachments_ids(self):
        return set(self.current_attachments_map.keys())

    @cached_property
    def backend_attachments_map(self):
        return {
            six.text_type(attachment.id): attachment
            for attachment in self.backend_issue.fields.attachment
        }

    @cached_property
    def backend_attachments_ids(self):
        return set(self.backend_attachments_map.keys())

    @cached_property
    def stale_attachment_ids(self):
        return self.current_attachments_ids - self.backend_attachments_ids

    @cached_property
    def new_attachment_ids(self):
        return self.backend_attachments_ids - self.current_attachments_ids

    @cached_property
    def updated_attachments_ids(self):
        return filter(self._is_attachment_updated, self.backend_attachments_ids)

    def _is_attachment_updated(self, attachment_id):
        """
        Attachment is considered updated if its thumbnail just has been created.
        """

        if not getattr(self.get_backend_attachment(attachment_id), 'thumbnail', False):
            return False

        if attachment_id not in self.current_attachments_ids:
            return False

        if self.get_current_attachment(attachment_id).thumbnail:
            return False

        return True

    def _download_file(self, url):
        """
        Download file from URL using secure JIRA session.
        :return: byte stream
        :raises: requests.RequestException
        """
        session = self.backend.manager._session
        response = session.get(url)
        response.raise_for_status()
        return six.BytesIO(response.content)

    def _add_attachment(self, issue, backend_attachment):
        attachment = self.backend.model_attachment(issue=issue,
                                                   backend_id=backend_attachment.id,
                                                   state=StateMixin.States.OK)
        thumbnail = getattr(backend_attachment, 'thumbnail', False) and getattr(attachment, 'thumbnail', False)

        try:
            content = self._download_file(backend_attachment.content)
            if thumbnail:
                thumbnail_content = self._download_file(backend_attachment.thumbnail)

        except JIRAError as error:
            logger.error('Unable to load attachment for issue with backend id {backend_id}. Error: {error}).'
                         .format(backend_id=issue.backend_id, error=error))
            return

        self.backend._backend_attachment_to_attachment(backend_attachment, attachment)

        try:
            attachment.save()
        except IntegrityError:
            logger.debug('Unable to create attachment issue_id=%s, backend_id=%s, '
                         'because it already exists in Waldur.', issue.id, backend_attachment.id)

        attachment.file.save(backend_attachment.filename, content, save=True)

        if thumbnail:
            attachment.thumbnail.save(backend_attachment.filename, thumbnail_content, save=True)

    def _update_attachment(self, issue, backend_attachment, current_attachment):
        try:
            content = self._download_file(backend_attachment.thumbnail)
        except JIRAError as error:
            logger.error('Unable to load attachment thumbnail for issue with backend id {backend_id}. Error: {error}).'
                         .format(backend_id=issue.backend_id, error=error))
            return

        current_attachment.thumbnail.save(backend_attachment.filename, content, save=True)


class CommentSynchronizer(object):
    def __init__(self, backend, current_issue, backend_issue):
        self.backend = backend
        self.current_issue = current_issue
        self.backend_issue = backend_issue

    def perform_update(self):
        if self.stale_comments_ids:
            self.backend.model_comment.objects.filter(backend_id__in=self.stale_comments_ids).delete()

    def get_current_comment(self, comment_id):
        return self.current_comments_map[comment_id]

    def get_backend_comment(self, comment_id):
        return self.backend_comments_map[comment_id]

    @cached_property
    def current_comments_map(self):
        return {
            six.text_type(comment.backend_id): comment
            for comment in self.current_issue.comments.all()
        }

    @cached_property
    def current_comments_ids(self):
        return set(self.current_comments_map.keys())

    @cached_property
    def backend_comments_map(self):
        return {
            six.text_type(comment.id): comment
            for comment in self.backend_issue.fields.comment.comments
        }

    @cached_property
    def backend_comments_ids(self):
        return set(self.backend_comments_map.keys())

    @cached_property
    def stale_comments_ids(self):
        return self.current_comments_ids - self.backend_comments_ids
