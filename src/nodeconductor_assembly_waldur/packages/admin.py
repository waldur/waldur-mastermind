import collections

from decimal import Decimal
from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.admin import widgets
from django.forms.models import BaseInlineFormSet

from nodeconductor_assembly_waldur.packages import models


class GBtoMBWidget(widgets.AdminIntegerFieldWidget):
    def value_from_datadict(self, data, files, name):
        value = super(GBtoMBWidget, self).value_from_datadict(data, files, name) or 0
        value = int(value) * 1024
        return value

    def _format_value(self, value):
        return int(value) / 1024

    def render(self, name, value, attrs=None):
        result = super(GBtoMBWidget, self).render(name, value, attrs)
        return '<label>%s GB</label>' % result


def render_to_readonly(value):
    return "<p>{0}</p>".format(value)


class ReadonlyNumberWidget(forms.NumberInput):
    def _format_value(self, value):
        return value

    def render(self, name, value, attrs=None):
        return render_to_readonly(self._format_value(value))


class PriceForMBinGBWidget(forms.NumberInput):
    def __init__(self, attrs):
        self.readonly = attrs.pop('readonly', False)
        super(PriceForMBinGBWidget, self).__init__(attrs)

    def value_from_datadict(self, data, files, name):
        value = super(PriceForMBinGBWidget, self).value_from_datadict(data, files, name) or 0
        value = Decimal(value) / 1024
        return value

    def _format_value(self, value):
        return Decimal(value) * 1024

    def render(self, name, value, attrs=None):
        if self.readonly:
            return render_to_readonly(self._format_value(value))
        else:
            return super(PriceForMBinGBWidget, self).render(name, value, attrs)


class PackageComponentForm(forms.ModelForm):
    monthly_price = forms.DecimalField(label='Price for 30 days', initial=0, required=True)
    price = forms.DecimalField(initial=0, label='Price per unit per day', required=False, widget=ReadonlyNumberWidget())
    if settings.DEBUG:
        price_per_day = forms.DecimalField(label='Price per day for MB',
                                           initial=0,
                                           required=False,
                                           widget=ReadonlyNumberWidget())

    class Meta:
        model = models.PackageComponent
        fields = ('type', 'amount', 'monthly_price', 'price')

    def __init__(self, *args, **kwargs):
        super(PackageComponentForm, self).__init__(*args, **kwargs)
        if self.instance:
            self.fields['monthly_price'].initial = self.instance.monthly_price
            if self.instance.type in models.PackageTemplate.get_memory_types():
                self.fields['price'].widget = PriceForMBinGBWidget(attrs={'readonly': True})

            if settings.DEBUG:
                self.fields['price_per_day'].initial = self.instance.price

    def clean(self):
        super(PackageComponentForm, self).clean()
        if 'monthly_price' not in self.cleaned_data or 'amount' not in self.cleaned_data:
            return

        instance = getattr(self, 'instance', None)
        template = getattr(instance, 'template', None)
        if template and template.openstack_packages.exists() and (
                        'monthly_price' in self.changed_data or 'amount' in self.changed_data):
            raise forms.ValidationError('Price cannot be changed for a template which has connected packages.')

        type = self.cleaned_data['type']
        monthly_price = self.cleaned_data['monthly_price']
        amount = self.cleaned_data['amount']

        price_min = 10 ** -models.PackageComponent.PRICE_DECIMAL_PLACES
        monthly_price_min = price_min * 30 * amount
        if monthly_price < monthly_price_min and monthly_price != 0:
            raise forms.ValidationError('Monthly price for "%s" should be greater than %s or equal to 0' % (
                type, monthly_price_min))

        price_max = 10 ** (models.PackageComponent.PRICE_MAX_DIGITS - models.PackageComponent.PRICE_DECIMAL_PLACES)
        monthly_price_max = price_max * 30 * amount
        if monthly_price > monthly_price_max:
            raise forms.ValidationError('Monthly price for "%s" should be lower than %s' % (type, monthly_price_max))

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
            form.fields['amount'] = forms.IntegerField(min_value=0, initial=0, widget=GBtoMBWidget())

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
            raise forms.ValidationError('One or more items are duplicated: %s' % ', '.join(duplicates))

        for t in models.PackageTemplate.get_required_component_types():
            if t not in filled:
                raise forms.ValidationError('%s component must be specified.' % t.capitalize())
            elif t in marked_for_delete:
                raise forms.ValidationError('%s component is required and cannot be deleted.' % t.capitalize())


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
    inlines = [PackageComponentInline]
    fields = ('name', 'category', 'description', 'archived', 'icon_url', 'service_settings')
    list_display = ('name', 'uuid', 'service_settings', 'price', 'archived', 'monthly_price', 'category')
    list_filter = ('service_settings',)
    search_fields = ('name', 'uuid')


class OpenStackPackageAdmin(admin.ModelAdmin):
    list_display = ('template', 'tenant', 'service_settings')
    list_filter = ('template',)


admin.site.register(models.PackageTemplate, PackageTemplateAdmin)
admin.site.register(models.OpenStackPackage, OpenStackPackageAdmin)
