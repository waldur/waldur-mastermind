from django import forms
from django.contrib import admin
from django.core import urlresolvers

from . import models


class JupyterHubManagementAdminForm(forms.ModelForm):
    class Meta:
        model = models.JupyterHubManagement
        fields = ('session_time_to_live_hours',)
        readonly_fields = ('uuid', 'created', 'python_management', 'user', 'instance_content_type', 'instance_object_id', 'project')


class JupyterHubManagementAdmin(admin.ModelAdmin):
    list_filter = ('session_time_to_live_hours',)
    list_display = ('uuid', 'created', 'instance_content_type', 'instance_object_id', 'python_management_link', 'project', 'user')
    list_display_links = ('uuid',)

    def python_management_link(self, obj):
        link = urlresolvers.reverse("admin:python_management_pythonmanagement_change", args=[obj.id])
        return '<a href="%s">%s</a>' % (link, obj.python_management.virtual_envs_dir_path)

    python_management_link.allow_tags = True
    python_management_link.short_description = 'Python Management'


class JupyterHubOAuthConfigAdminForm(forms.ModelForm):
    class Meta:
        model = models.JupyterHubOAuthConfig
        fields = ('type', 'oauth_callback_url', 'client_id', 'client_secret', 'tenant_id', 'gitlab_host')
        readonly_fields = ('uuid', 'created')


class JupyterHubOAuthConfigAdmin(admin.ModelAdmin):
    list_filter = ('type', 'oauth_callback_url', 'gitlab_host')
    list_display = ('uuid', 'type', 'oauth_callback_url', 'gitlab_host', 'jupyter_hub_management_link')
    list_display_links = ('uuid',)

    def jupyter_hub_management_link(self, obj):
        link = urlresolvers.reverse("admin:jupyter_hub_management_jupyterhubmanagement_change", args=[obj.id])
        return '<a href="%s">JupyterHub management</a>' % link

    jupyter_hub_management_link.allow_tags = True
    jupyter_hub_management_link.short_description = 'JupyterHub Management'


class RequestAdminForm(forms.ModelForm):
    class Meta:
        fields = ('state', 'output')
        readonly_fields = ('uuid', 'created', 'output')


class RequestAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'created', 'state', 'jupyter_hub_management_link')
    list_display_links = ('uuid',)

    def jupyter_hub_management_link(self, obj):
        link = urlresolvers.reverse("admin:jupyter_hub_management_jupyterhubmanagement_change", args=[obj.id])
        return '<a href="%s">JupyterHub management</a>' % link

    jupyter_hub_management_link.allow_tags = True
    jupyter_hub_management_link.short_description = 'JupyterHub Management'


class JupyterHubManagementSyncConfigurationRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.JupyterHubManagementSyncConfigurationRequest


class JupyterHubManagementSyncConfigurationRequestAdmin(RequestAdmin):
    pass


class JupyterHubManagementMakeVirtualEnvironmentGlobalRequestForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest


class JupyterHubManagementMakeVirtualEnvironmentGlobalRequestAdmin(RequestAdmin):
    pass


class JupyterHubManagementDeleteRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.JupyterHubManagementDeleteRequest


class JupyterHubManagementDeleteRequestAdmin(RequestAdmin):
    pass


class JupyterHubManagementMakeVirtualEnvironmentLocalRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest


class JupyterHubManagementMakeVirtualEnvironmentLocalRequestAdmin(RequestAdmin):
    pass


admin.site.register(models.JupyterHubManagement, JupyterHubManagementAdmin)
admin.site.register(models.JupyterHubOAuthConfig, JupyterHubOAuthConfigAdmin)
admin.site.register(models.JupyterHubManagementSyncConfigurationRequest, JupyterHubManagementSyncConfigurationRequestAdmin)
admin.site.register(models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest, JupyterHubManagementMakeVirtualEnvironmentGlobalRequestAdmin)
admin.site.register(models.JupyterHubManagementDeleteRequest, JupyterHubManagementDeleteRequestAdmin)
admin.site.register(models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest, JupyterHubManagementMakeVirtualEnvironmentLocalRequestAdmin)
