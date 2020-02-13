from django.contrib import admin
from django.utils.translation import ugettext_lazy as _
from django.shortcuts import redirect
from django.urls import reverse

from waldur_core.core import admin as core_admin
from waldur_core.structure import admin as structure_admin

from . import models, tasks


class RancherUserClusterLinkInline(admin.TabularInline):
    model = models.RancherUserClusterLink


class RancherUserAdmin(core_admin.ExtraActionsMixin, core_admin.ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('__str__', 'settings', 'is_active')

    inlines = [
        RancherUserClusterLinkInline,
    ]

    def get_extra_actions(self):
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


admin.site.register(models.RancherService, structure_admin.ServiceAdmin)
admin.site.register(models.RancherServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(models.Cluster)
admin.site.register(models.Node)
admin.site.register(models.RancherUser, RancherUserAdmin)
admin.site.register(models.Catalog, CatalogAdmin)
