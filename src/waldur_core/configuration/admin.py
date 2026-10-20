from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.utils.translation import gettext_lazy as _

from waldur_core.configuration import registry
from waldur_core.configuration.forms import SettingsForm
from waldur_core.configuration.models import Setting


class SettingAdmin(admin.ModelAdmin):
    """One page with every setting, grouped by fieldset, instead of a list of
    rows: a setting nobody has changed has no row but is still editable."""

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # The per-row form would edit the stored JSON past Setting.objects.set:
        # secrets in clear, no setting_updated, no record of who changed it.
        raise PermissionDenied

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            return self.admin_site.login(request)
        can_change = self.has_change_permission(request)
        initial = Setting.objects.values_with_defaults()
        if request.method == "POST" and can_change:
            form = SettingsForm(request.POST, request.FILES, initial=initial)
            if form.is_valid():
                form.save(user=request.user)
                messages.success(request, _("Settings have been saved."))
                return redirect(request.path)
        else:
            form = SettingsForm(initial=initial)
        if not can_change:
            for field in form.fields.values():
                field.disabled = True

        fieldsets = [
            (title, [form[key] for key in keys if key in form.fields])
            for title, keys in registry.fieldsets().items()
        ]
        context = {
            **self.admin_site.each_context(request),
            "title": _("Settings"),
            "opts": self.model._meta,
            "form": form,
            "fieldsets": fieldsets,
            "can_change": can_change,
            **(extra_context or {}),
        }
        return TemplateResponse(
            request, "admin/configuration/setting/change_list.html", context
        )


admin.site.register(Setting, SettingAdmin)
