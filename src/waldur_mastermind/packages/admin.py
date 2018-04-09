from __future__ import unicode_literals

import collections
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from waldur_core.structure import models as structure_models
from waldur_mastermind.packages import models, utils


class PriceForMBinGBWidget(forms.NumberInput):
    def __init__(self, attrs):
        self.readonly = attrs.pop('readonly', False)
        super(PriceForMBinGBWidget, self).__init__(attrs)

    def value_from_datadict(self, data, files, name):
        value = super(PriceForMBinGBWidget, self).value_from_datadict(data, files, name) or 0
        value = Decimal(value) / 1024
        return value

    def format_value(self, value):
        return Decimal(value) * 1024

    def render(self, name, value, attrs=None, renderer=None):
        if self.readonly:
            return core_admin.render_to_readonly(self._format_value(value))
        else:
            return super(PriceForMBinGBWidget, self).render(name, value, attrs)


class PackageComponentForm(forms.ModelForm):
    monthly_price = forms.DecimalField(label=_('Price for 30 days'), initial=0, required=True)
    price = forms.DecimalField(initial=0, label=_('Price per unit per day'), required=False,
                               widget=core_admin.ReadonlyTextWidget())
    if settings.DEBUG:
        price_per_day = forms.DecimalField(label=_('Price per day for MB'),
                                           initial=0,
                                           required=False,
                                           widget=core_admin.ReadonlyTextWidget())

    class Meta:
        model = models.PackageComponent
        price_defining_fields = ('amount', 'monthly_price')
        fields = ('type', 'price') + price_defining_fields

    def __init__(self, *args, **kwargs):
        super(PackageComponentForm, self).__init__(*args, **kwargs)
        if self.instance:
            self.fields['monthly_price'].initial = self.instance.monthly_price
            if self.instance.type in models.PackageTemplate.get_memory_types():
                self.fields['price'].widget = PriceForMBinGBWidget(attrs={'readonly': True})

            if settings.DEBUG:
                self.fields['price_per_day'].initial = self.instance.price

            if hasattr(self.instance, 'template') and self.instance.template.openstack_packages.exists():
                for field in (field for field in self.fields if field in self.Meta.price_defining_fields):
                    self.fields[field].widget.attrs['readonly'] = True

    def clean(self):
        super(PackageComponentForm, self).clean()
        if 'monthly_price' not in self.cleaned_data or 'amount' not in self.cleaned_data:
            return

        instance = getattr(self, 'instance', None)
        template = getattr(instance, 'template', None)
        if template and template.openstack_packages.exists() and (
                'monthly_price' in self.changed_data or 'amount' in self.changed_data):
            raise forms.ValidationError(_('Price cannot be changed for a template which has connected packages.'))

        type = self.cleaned_data['type']
        monthly_price = self.cleaned_data['monthly_price']
        amount = self.cleaned_data['amount']

        price_min = 10 ** -models.PackageComponent.PRICE_DECIMAL_PLACES
        monthly_price_min = price_min * 30 * amount
        if monthly_price < monthly_price_min and monthly_price != 0:
            raise forms.ValidationError(_('Monthly price for "%(type)s" should be greater than %(min)s or equal to 0') % {
                'type': type,
                'min': monthly_price_min,
            })

        price_max = 10 ** (models.PackageComponent.PRICE_MAX_DIGITS - models.PackageComponent.PRICE_DECIMAL_PLACES)
        monthly_price_max = price_max * 30 * amount
        if monthly_price > monthly_price_max:
            raise forms.ValidationError(_('Monthly price for "%(type)s" should be lower than %(max)s') % {
                'type': type,
                'max': monthly_price_max
            })

    def save(self, commit=True):
        monthly_price = self.cleaned_data.get('monthly_price', None)
        amount = self.cleaned_data.get('amount', 0)
        package_component = super(PackageComponentForm, self).save(commit=commit)
        if amount:
            package_component.price = monthly_price / 30 / amount
        if commit:
            package_component.save()
        return package_component


class PackageComponentInlineFormset(BaseInlineFormSet):
    """
    Formset responsible for package component inlines validation and their initial population.
    """
    form = PackageComponentForm

    def __init__(self, **kwargs):
        # Fill inlines with required component types
        kwargs['initial'] = [{'type': t} for t in models.PackageTemplate.get_required_component_types()]
        super(PackageComponentInlineFormset, self).__init__(**kwargs)

    def add_fields(self, form, index):
        super(PackageComponentInlineFormset, self).add_fields(form, index)
        if 'type' in form.initial and form.initial['type'] in models.PackageTemplate.get_memory_types():
            is_readonly = self.instance and self.instance.is_read_only()
            form.fields['amount'] = forms.IntegerField(min_value=1,
                                                       initial=1024,
                                                       widget=core_admin.GBtoMBWidget({'readonly': is_readonly}))
        elif 'type' in form.initial and form.initial['type'] == models.PackageComponent.Types.CORES:
            form.fields['amount'] = forms.IntegerField(min_value=1, initial=1)

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
            raise forms.ValidationError(_('One or more items are duplicated: %s') % ', '.join(duplicates))

        for t in models.PackageTemplate.get_required_component_types():
            if t not in filled:
                raise forms.ValidationError(_('%s component must be specified.') % t.capitalize())
            elif t in marked_for_delete:
                raise forms.ValidationError(_('%s component is required and cannot be deleted.') % t.capitalize())


class PackageComponentInline(admin.TabularInline):
    form = PackageComponentForm
    formset = PackageComponentInlineFormset
    model = models.PackageComponent
    extra = 0

    def get_extra(self, request, obj=None, **kwargs):
        if obj:
            return super(PackageComponentInline, self).get_extra(request, obj, **kwargs)
        # On creation number of inlines must be equal to number of required components
        return len(models.PackageTemplate.get_required_component_types())


class PackageTemplateAdmin(admin.ModelAdmin):
    # WIKI: https://opennode.atlassian.net/wiki/display/WD/Shared+OpenStack+Provider+Management#SharedOpenStackProviderManagement-VPCPackagetemplatemanagement
    inlines = [PackageComponentInline]
    package_dependant_fields = ('name', 'category', 'service_settings')
    fields = package_dependant_fields + ('archived', 'icon_url', 'description', 'product_code', 'article_code')
    list_display = ('name', 'uuid', 'service_settings', 'price', 'archived', 'monthly_price', 'category')

    class ServiceSettingsSharedFilter(admin.RelatedFieldListFilter):
        def field_choices(self, field, request, model_admin):
            return field.get_choices(include_blank=False, limit_choices_to={'shared': True})

    list_filter = (('service_settings', ServiceSettingsSharedFilter),)
    search_fields = ('name', 'uuid')

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.is_read_only():
            return self.package_dependant_fields
        else:
            return super(PackageTemplateAdmin, self).get_readonly_fields(request, obj)

    def get_form(self, request, obj=None, **kwargs):
        form = super(PackageTemplateAdmin, self).get_form(request, obj, **kwargs)

        if 'service_settings' in form.base_fields:
            form.base_fields['service_settings'].queryset = structure_models.ServiceSettings.objects.filter(shared=True)
        return form

    def save_related(self, request, form, formsets, change):
        super(PackageTemplateAdmin, self).save_related(request, form, formsets, change)
        utils.sync_price_list_item(form.instance)


class OpenStackPackageAdmin(admin.ModelAdmin):
    def get_project(self, obj):
        return obj.tenant.service_project_link.project.name

    get_project.short_description = _('Project')
    get_project.admin_order_field = 'tenant__service_project_link__project__name'

    def get_customer(self, obj):
        return obj.tenant.service_project_link.project.customer

    get_customer.short_description = _('Organization')
    get_customer.admin_order_field = 'tenant__service_project_link__project__customer__name'

    list_display = ('template', 'tenant', 'service_settings', 'get_project', 'get_customer')
    list_filter = ('template',)


admin.site.register(models.PackageTemplate, PackageTemplateAdmin)
admin.site.register(models.OpenStackPackage, OpenStackPackageAdmin)
