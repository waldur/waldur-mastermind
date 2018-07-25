from django import forms
from django.contrib import admin
from django.core import urlresolvers

from . import models


class CachedRepositoryPythonLibraryAdminForm(forms.ModelForm):
    class Meta:
        model = models.CachedRepositoryPythonLibrary
        fields = ('name',)


class CachedRepositoryPythonLibraryAdmin(admin.ModelAdmin):
    list_filter = ('name',)
    list_display = ('name',)


class PythonManagementAdminForm(forms.ModelForm):
    class Meta:
        model = models.PythonManagement
        fields = ('system_user', 'project', 'user')
        readonly_fields = ('uuid', 'created', 'virtual_envs_dir_path', 'instance_content_type', 'instance_object_id', 'python_version')


class PythonManagementAdmin(admin.ModelAdmin):
    list_filter = ('virtual_envs_dir_path', 'python_version', 'system_user', 'project', 'user')
    list_display = ('uuid', 'created', 'instance_content_type', 'instance_object_id', 'virtual_envs_dir_path', 'python_version', 'system_user', 'project', 'user')
    list_display_links = ('uuid',)


class RequestAdminForm(forms.ModelForm):
    class Meta:
        fields = ('state', 'output')
        readonly_fields = ('uuid', 'created', 'output')


class RequestAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'created', 'state', 'python_management_link')
    list_display_links = ('uuid',)

    def python_management_link(self, obj):
        link = urlresolvers.reverse("admin:python_management_pythonmanagement_change", args=[obj.id])
        return '<a href="%s">%s</a>' % (link, obj.python_management.virtual_envs_dir_path)

    python_management_link.allow_tags = True
    python_management_link.short_description = 'Python Management'


class PythonManagementInitializeRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.PythonManagementInitializeRequest


class PythonManagementInitializeRequestAdmin(RequestAdmin):
    pass


class PythonManagementSynchronizeRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.PythonManagementSynchronizeRequest


class PythonManagementSynchronizeRequestAdmin(RequestAdmin):
    pass


class PythonManagementDeleteRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.PythonManagementDeleteRequest


class PythonManagementDeleteRequestAdmin(RequestAdmin):
    pass


class PythonManagementDeleteVirtualEnvRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.PythonManagementDeleteVirtualEnvRequest


class PythonManagementDeleteVirtualEnvRequestAdmin(RequestAdmin):
    pass


class PythonManagementFindVirtualEnvsRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.PythonManagementFindVirtualEnvsRequest


class PythonManagementFindVirtualEnvsRequestAdmin(RequestAdmin):
    pass


class PythonManagementFindInstalledLibrariesRequestAdminForm(RequestAdminForm):
    class Meta(RequestAdminForm.Meta):
        model = models.PythonManagementFindInstalledLibrariesRequest


class PythonManagementFindInstalledLibrariesRequestAdmin(RequestAdmin):
    pass


admin.site.register(models.CachedRepositoryPythonLibrary, CachedRepositoryPythonLibraryAdmin)
admin.site.register(models.PythonManagement, PythonManagementAdmin)
admin.site.register(models.PythonManagementInitializeRequest, PythonManagementInitializeRequestAdmin)
admin.site.register(models.PythonManagementSynchronizeRequest, PythonManagementSynchronizeRequestAdmin)
admin.site.register(models.PythonManagementDeleteRequest, PythonManagementDeleteRequestAdmin)
admin.site.register(models.PythonManagementDeleteVirtualEnvRequest, PythonManagementDeleteVirtualEnvRequestAdmin)
admin.site.register(models.PythonManagementFindVirtualEnvsRequest, PythonManagementFindVirtualEnvsRequestAdmin)
admin.site.register(models.PythonManagementFindInstalledLibrariesRequest, PythonManagementFindInstalledLibrariesRequestAdmin)
