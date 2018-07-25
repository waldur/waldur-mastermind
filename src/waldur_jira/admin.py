from django.contrib import admin
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from waldur_core.structure import admin as structure_admin
from django.core.exceptions import ValidationError

from . import executors, models


class JiraPropertyAdmin(core_admin.UpdateOnlyModelAdmin,
                        structure_admin.BackendModelAdmin,
                        admin.ModelAdmin):
    list_display = ('name', 'description', 'settings')
    search_fields = ('name', 'description')


class ProjectTemplateAdmin(JiraPropertyAdmin):
    list_display = ('name', 'description')
    search_fields = ('name', 'description')


class ProjectAdmin(structure_admin.ResourceAdmin):
    actions = ['pull']

    class Pull(core_admin.ExecutorAdminAction):
        executor = executors.ProjectPullExecutor
        short_description = _('Pull')

        def validate(self, service_settings):
            States = models.Project.States
            if service_settings.state not in (States.OK, States.ERRED):
                raise ValidationError(_('Project has to be OK or erred.'))

    pull = Pull()


class IssueAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('project',)
    list_display = ('backend_id', 'type', 'project', 'status', 'reporter_name', 'assignee_name')
    actions = ['pull']

    class Pull(core_admin.ExecutorAdminAction):
        executor = executors.IssueUpdateFromBackendExecutor
        short_description = _('Pull issue')

    pull = Pull()


admin.site.register(models.Priority, JiraPropertyAdmin)
admin.site.register(models.IssueType, JiraPropertyAdmin)
admin.site.register(models.ProjectTemplate, ProjectTemplateAdmin)
admin.site.register(models.Issue, IssueAdmin)
admin.site.register(models.Comment, admin.ModelAdmin)
admin.site.register(models.Project, ProjectAdmin)
admin.site.register(models.JiraService, structure_admin.ServiceAdmin)
admin.site.register(models.JiraServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
