from django.contrib import admin

from . import models


class BusySlotAdmin(admin.ModelAdmin):
    list_display = ('offering', 'start', 'end')


admin.site.register(models.BusySlot, BusySlotAdmin)
