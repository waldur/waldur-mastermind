from django.contrib import admin

from waldur_core.users import models


class InvitationAdmin(admin.ModelAdmin):
    list_display = ('email', 'state', 'created', 'created_by', 'customer', 'project')
    list_filter = ('state', 'created')
    search_fields = ('email', 'customer__name')


class GroupInvitationAdmin(admin.ModelAdmin):
    list_display = ('created', 'created_by', 'customer', 'project')
    list_filter = ('created',)
    search_fields = ('customer__name', 'project__name')


admin.site.register(models.Invitation, InvitationAdmin)
admin.site.register(models.GroupInvitation, GroupInvitationAdmin)
