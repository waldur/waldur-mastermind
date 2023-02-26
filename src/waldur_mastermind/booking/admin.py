from django import forms
from django.contrib import admin

from . import models


class BusySlotAdmin(admin.ModelAdmin):
    list_display = ('offering', 'start', 'end')


class BookingSlotForm(forms.ModelForm):
    class Meta:
        model = models.BookingSlot
        exclude = ('resource',)


class BookingSlotAdmin(admin.ModelAdmin):
    list_display = ('resource', 'start', 'end')
    form = BookingSlotForm


admin.site.register(models.BusySlot, BusySlotAdmin)
admin.site.register(models.BookingSlot, BookingSlotAdmin)
