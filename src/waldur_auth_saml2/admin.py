from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from . import models, tasks


class IdentityProviderAdmin(core_admin.ExtraActionsMixin, admin.ModelAdmin):
    fields = ('name', 'url')
    readonly_fields = ('name', 'url')
    list_display = ('name',)
    search_fields = ('name',)

    def get_extra_actions(self):
        return [
            self.sync_providers,
        ]

    def sync_providers(self, request):
        tasks.sync_providers()
        self.message_user(request, _('Identity providers have been synchronized.'))
        return redirect(reverse('admin:waldur_auth_saml2_identityprovider_changelist'))


admin.site.register(models.IdentityProvider, IdentityProviderAdmin)
