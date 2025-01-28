from django import forms
from django.contrib import admin

from waldur_autoprovisioning import models
from waldur_mastermind.marketplace import models as marketplace_models


class RulePlansInlineForm(forms.ModelForm):
    class Meta:
        model = models.RulePlans
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = marketplace_models.Plan.objects.select_related(
            "offering"
        ).order_by("offering__name", "name")
        self.fields["plan"].label_from_instance = (
            lambda obj: f"{obj.offering.name} | {obj.name}"
        )


class RulePlansInline(admin.TabularInline):
    model = models.RulePlans
    form = RulePlansInlineForm
    extra = 1


class RuleAdmin(admin.ModelAdmin):
    inlines = [RulePlansInline]
    list_display = ("customer", "get_offering_names")

    def get_offering_names(self, obj):
        return ", ".join(set(plan.offering.name for plan in obj.plans.all()))

    get_offering_names.short_description = "Offerings"


admin.site.register(models.Rule, RuleAdmin)
