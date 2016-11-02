from django import forms
from django.contrib import admin

from . import models


class OpenStackItemInline(admin.TabularInline):
    model = models.OpenStackItem
    extra = 0


class InvoiceForm(forms.ModelForm):
    def clean(self):
        super(InvoiceForm, self).clean()
        if any(self._errors):
            return

        customer = self.cleaned_data.get('customer')
        year = self.cleaned_data.get('year')
        month = self.cleaned_data.get('month')

        if models.Invoice.objects.filter(
                customer=customer,
                month=month,
                year=year,
                state=models.Invoice.States.PENDING).exists():
            raise forms.ValidationError(
                'Pending invoice for customer %s in %d-%d already exists.' % (customer, year, month))


class InvoiceAdmin(admin.ModelAdmin):
    form = InvoiceForm
    inlines = [OpenStackItemInline]
    fields = ('customer', 'state', 'total', 'year', 'month')
    readonly_fields = ('total',)
    list_display = ('customer', 'total', 'year', 'month', 'state')
    list_filter = ('state', 'customer')
    search_fields = ('customer', 'uuid')


admin.site.register(models.Invoice, InvoiceAdmin)
