from django import forms
from django.contrib import admin

from waldur_autoprovisioning import models
from waldur_core.permissions.models import Role
from waldur_mastermind.marketplace import models as marketplace_models


class RuleForm(forms.ModelForm):
    class Meta:
        model = models.Rule
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project_role"].queryset = Role.project_roles()
        if "plan" in self.fields:
            self.fields[
                "plan"
            ].queryset = marketplace_models.Plan.objects.select_related(
                "offering"
            ).order_by("offering__name", "name")
            self.fields["plan"].label_from_instance = (
                lambda obj: f"{obj.offering.name} | {obj.name}"
            )


class RuleAdmin(admin.ModelAdmin):
    form = RuleForm
    list_display = ("customer", "project_role", "get_offering_name")

    def get_offering_name(self, obj):
        return obj.plan.offering.name if obj.plan else "No plan"

    get_offering_name.short_description = "Offering"


admin.site.register(models.Rule, RuleAdmin)
