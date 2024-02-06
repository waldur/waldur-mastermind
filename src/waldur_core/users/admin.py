from django.contrib import admin

from waldur_core.users import models


class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "state", "created", "created_by", "customer", "scope")
    list_filter = ("state", "created")
    search_fields = ("email", "customer__name")


class GroupInvitationAdmin(admin.ModelAdmin):
    list_display = ("created", "created_by", "customer", "scope")
    list_filter = ("created",)
    search_fields = ("customer__name",)


class PermissionRequestAdmin(admin.ModelAdmin):
    list_display = ("created", "created_by", "invitation", "state")
    list_filter = ("created",)
    search_fields = ("invitation__customer__name",)


admin.site.register(models.Invitation, InvitationAdmin)
admin.site.register(models.GroupInvitation, GroupInvitationAdmin)
admin.site.register(models.PermissionRequest, PermissionRequestAdmin)
