from django.contrib import admin

from . import models


class RequestedOfferingInline(admin.TabularInline):
    model = models.RequestedOffering
    extra = 1


class RoundInline(admin.TabularInline):
    model = models.Round
    extra = 1


class CallReviewersInline(admin.TabularInline):
    model = models.CallReviewer
    extra = 1


class CallAdmin(admin.ModelAdmin):
    inlines = [RequestedOfferingInline, RoundInline, CallReviewersInline]
    list_display = ('name',)


class RoundAdmin(admin.ModelAdmin):
    list_display = ('call', 'start_time', 'cutoff_time')


class ProposalAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'get_state_display')


class ReviewAdmin(admin.ModelAdmin):
    list_display = ('reviewer', 'proposal')


admin.site.register(models.CallManagingOrganisation)
admin.site.register(models.Call, CallAdmin)
admin.site.register(models.Round, RoundAdmin)
admin.site.register(models.Proposal, ProposalAdmin)
admin.site.register(models.Review, ReviewAdmin)
