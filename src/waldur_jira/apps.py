from django.apps import AppConfig
from django.db.models import signals


class JiraConfig(AppConfig):
    name = 'waldur_jira'
    verbose_name = 'JIRA'
    service_name = 'JIRA'

    def ready(self):
        from waldur_core.quotas import fields as quota_fields
        from waldur_core.structure import SupportedServices
        from waldur_core.structure import models as structure_models
        from waldur_core.structure.signals import resource_imported

        from . import handlers
        from .backend import JiraBackend
        SupportedServices.register_backend(JiraBackend)

        Issue = self.get_model('Issue')
        Comment = self.get_model('Comment')
        Project = self.get_model('Project')

        structure_models.Project.add_quota_field(
            name='nc_jira_project_count',
            quota_field=quota_fields.CounterQuotaField(
                target_models=lambda: [Project],
                path_to_scope='service_project_link.project',
            )
        )

        resource_imported.connect(
            handlers.import_project_issues,
            sender=Project,
            dispatch_uid='waldur_jira.handlers.import_project_issues',
        )

        signals.post_save.connect(
            handlers.log_issue_save,
            sender=Issue,
            dispatch_uid='waldur_jira.handlers.log_issue_save',
        )

        signals.post_delete.connect(
            handlers.log_issue_delete,
            sender=Issue,
            dispatch_uid='waldur_jira.handlers.log_issue_delete',
        )

        signals.post_save.connect(
            handlers.log_comment_save,
            sender=Comment,
            dispatch_uid='waldur_jira.handlers.log_comment_save',
        )

        signals.post_delete.connect(
            handlers.log_comment_delete,
            sender=Comment,
            dispatch_uid='waldur_jira.handlers.log_comment_delete',
        )
