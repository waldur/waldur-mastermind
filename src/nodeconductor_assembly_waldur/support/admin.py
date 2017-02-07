from django.contrib import admin

from . import models


class OfferingAdmin(admin.ModelAdmin):
    list_display = ('type', 'name', 'price', 'state')


admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Issue, admin.ModelAdmin)
admin.site.register(models.Comment, admin.ModelAdmin)
admin.site.register(models.SupportUser, admin.ModelAdmin)
