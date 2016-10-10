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

        for t in models.PackageTemplate.get_required_component_types():
            if t not in filled:
                raise ValidationError('%s component must be specified.' % t.capitalize())
            elif t in marked_for_delete:
                raise ValidationError('%s component is required and cannot be deleted.' % t.capitalize())


class PackageComponentInline(admin.TabularInline):
    formset = PackageComponentInlineFormset
    model = models.PackageComponent
    extra = 0

    def get_extra(self, request, obj=None, **kwargs):
        if obj:
            return super(PackageComponentInline, self).get_extra(request, obj, **kwargs)
        # On creation number of inlines must be equal to number of required components
        return len(models.PackageTemplate.get_required_component_types())


class PackageTemplateAdmin(admin.ModelAdmin):
    inlines = [PackageComponentInline]
    fields = ('name', 'description', 'type', 'icon_url')
    list_display = ('name', 'uuid', 'type', 'price')
    list_filter = ('type',)
    search_fields = ('name', 'uuid')

admin.site.register(models.PackageTemplate, PackageTemplateAdmin)
