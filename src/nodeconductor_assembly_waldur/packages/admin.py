import collections

from django.contrib import admin
from django.forms import ValidationError
from django.forms.models import BaseInlineFormSet

from nodeconductor_assembly_waldur.packages import models


class PackageComponentInlineFormset(BaseInlineFormSet):
    """
    Formset responsible for package component inlines validation and their initial population.
    """
    def __init__(self, **kwargs):
        # Fill inlines with required component types
        kwargs['initial'] = [{'type': t} for t in models.PackageTemplate.get_required_component_types()]
        super(PackageComponentInlineFormset, self).__init__(**kwargs)

    def clean(self):
        super(PackageComponentInlineFormset, self).clean()
        if any(self._errors):
            return
        # Check whether all required components were filled and are not marked for deletion.
        marked_for_delete = []
        filled = []
        for comp in self.cleaned_data:
            if not comp:
                continue
            elif comp.get('DELETE', False):
                marked_for_delete.append(comp['type'])
            filled.append(comp['type'])
        duplicates = [item for item, count in collections.Counter(filled).items() if count > 1]
        if duplicates:
            raise ValidationError('One or more items are duplicated: %s' % ', '.join(duplicates))

        for t in models.PackageTemplate.get_required_component_types():
            if t not in filled:
                raise ValidationError('%s component must be specified.' % t.capitalize())
            elif t in marked_for_delete:
                raise ValidationError('%s component is required and cannot be deleted.' % t.capitalize())


class PackageComponentInline(admin.TabularInline):
    formset = PackageComponentInlineFormset
    model = models.PackageComponent
    extra = 0
    fields = ('type', 'amount', 'price')

    def get_extra(self, request, obj=None, **kwargs):
        if obj:
            return super(PackageComponentInline, self).get_extra(request, obj, **kwargs)
        # On creation number of inlines must be equal to number of required components
        return len(models.PackageTemplate.get_required_component_types())


class PackageTemplateAdmin(admin.ModelAdmin):
    inlines = [PackageComponentInline]
    fields = ('name', 'description', 'icon_url', 'service_settings')
    list_display = ('name', 'uuid', 'service_settings', 'price')
    list_filter = ('service_settings',)
    search_fields = ('name', 'uuid')


class OpenStackPackageAdmin(admin.ModelAdmin):
    list_display = ('template', 'tenant', 'service_settings')
    list_filter = ('template',)


admin.site.register(models.PackageTemplate, PackageTemplateAdmin)
admin.site.register(models.OpenStackPackage, OpenStackPackageAdmin)
