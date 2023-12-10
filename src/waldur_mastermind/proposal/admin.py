from django.contrib import admin

from . import models


class RequestedOfferingInline(admin.TabularInline):
    model = models.RequestedOffering
    extra = 1


class RoundInline(admin.TabularInline):
    model = models.Round
    extra = 1


class CallAdmin(admin.ModelAdmin):
    inlines = [RequestedOfferingInline, RoundInline]
    list_display = ('name', 'start_time', 'end_time')


class RoundAdmin(admin.ModelAdmin):
    list_display = ('call', 'start_time', 'end_time')


class ProposalAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'get_state_display')


admin.site.register(models.CallManagingOrganisation)
admin.site.register(models.Call, CallAdmin)
admin.site.register(models.Round, RoundAdmin)
admin.site.register(models.Proposal, ProposalAdmin)
