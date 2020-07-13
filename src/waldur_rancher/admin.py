from django.conf import settings
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from waldur_core.structure import admin as structure_admin

from . import models, tasks


class RancherUserClusterLinkInline(admin.TabularInline):
    model = models.RancherUserClusterLink


class RancherUserProjectLinkInline(admin.TabularInline):
    model = models.RancherUserProjectLink


class RancherUserAdmin(core_admin.ExtraActionsMixin, admin.ModelAdmin):
    list_display = ('__str__', 'settings', 'is_active')

    inlines = [
        RancherUserClusterLinkInline,
        RancherUserProjectLinkInline,
    ]

    def get_extra_actions(self):
        if settings.WALDUR_RANCHER['READ_ONLY_MODE']:
            return []
        return [
            self.sync_users,
        ]

    def sync_users(self, request):
        tasks.sync_users.delay()
        self.message_user(request, _('Users\' synchronization has been scheduled.'))
        return redirect(reverse('admin:waldur_rancher_rancheruser_changelist'))


class CatalogAdmin(admin.ModelAdmin):
    list_display = ('name', 'scope_type', 'scope_name', 'catalog_url')

    def scope_name(self, obj):
        return obj.scope.name


class ClusterAdmin(structure_admin.ResourceAdmin):
    list_display = structure_admin.ResourceAdmin.list_display + ('runtime_state',)
    list_filter = structure_admin.ResourceAdmin.list_filter + ('tenant_settings',)


class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'cluster', 'runtime_state')


class NamespaceAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'runtime_state')


class TemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'catalog', 'runtime_state')
    list_filter = (
        'cluster',
        'catalog',
    )

    search_fields = ('name', 'description')


class ClusterTemplateNodeInline(admin.TabularInline):
    model = models.ClusterTemplateNode


class ClusterTemplateAdmin(core_admin.HideAdminOriginalMixin):
    list_display = ('name', 'description')
    inlines = [ClusterTemplateNodeInline]


admin.site.register(models.RancherService, structure_admin.ServiceAdmin)
admin.site.register(
    models.RancherServiceProjectLink, structure_admin.ServiceProjectLinkAdmin
)
admin.site.register(models.Cluster, ClusterAdmin)
admin.site.register(models.Node)
admin.site.register(models.RancherUser, RancherUserAdmin)
admin.site.register(models.Catalog, CatalogAdmin)
admin.site.register(models.Project, ProjectAdmin)
admin.site.register(models.Namespace, NamespaceAdmin)
admin.site.register(models.Template, TemplateAdmin)
admin.site.register(models.ClusterTemplate, ClusterTemplateAdmin)
