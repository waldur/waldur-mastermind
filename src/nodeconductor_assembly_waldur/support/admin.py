from django.contrib import admin

from nodeconductor.structure import admin as structure_admin

from . import models


class OfferingAdmin(admin.ModelAdmin):
    list_display = ('type', 'name', 'price', 'state')


class IssueAdmin(structure_admin.BackendModelAdmin):
    exclude = ('resource_content_type', 'resource_object_id')


admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Issue, IssueAdmin)
admin.site.register(models.Comment, structure_admin.BackendModelAdmin)
admin.site.register(models.SupportUser, admin.ModelAdmin)
