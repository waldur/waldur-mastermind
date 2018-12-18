from __future__ import unicode_literals

from django.contrib import admin
from django.forms.models import ModelForm
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import ugettext_lazy as _, ungettext
from jsoneditor.forms import JSONEditor

from waldur_core.core import admin as core_admin
from waldur_core.core.admin import format_json_field
from waldur_core.core.admin_filters import RelatedOnlyDropdownFilter
from waldur_core.structure.models import ServiceSettings, SharedServiceSettings, PrivateServiceSettings

from . import models, tasks


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class AttributeOptionInline(admin.TabularInline):
    model = models.AttributeOption


class AttributeAdmin(admin.ModelAdmin):
    inlines = [AttributeOptionInline]
    list_display = ('title', 'get_category', 'section', 'type', 'key', 'required',)
    list_filter = ('section',)
    ordering = ('section', 'title')

    def get_category(self, obj):
        return obj.section.category

    get_category.short_description = _('Category')
    get_category.admin_order_field = 'section__category__title'


class AttributeInline(admin.TabularInline):
    model = models.Attribute


class SectionAdmin(admin.ModelAdmin):
    inlines = [AttributeInline]
    list_display = ('title', 'category', 'key')


class SectionInline(admin.TabularInline):
    model = models.Section


class CategoryColumnInline(admin.TabularInline):
    model = models.CategoryColumn
    list_display = ('index', 'title', 'attribute', 'widget')


class CategoryAdmin(admin.ModelAdmin):
    list_display = ('title', 'uuid',)
    inlines = [SectionInline, CategoryColumnInline]


class ScreenshotsInline(admin.TabularInline):
    model = models.Screenshot
    fields = ('name', 'description', 'image')


class PlansInline(admin.TabularInline):
    model = models.Plan
    fields = ('name', 'description', 'unit_price', 'unit',
              'product_code', 'article_code', 'archived')


class PlanComponentInline(admin.TabularInline):
    model = models.PlanComponent


class PlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'offering', 'archived', 'unit', 'unit_price')
    list_filter = ('offering', 'archived')
    search_fields = ('name', 'offering__name')
    inlines = [PlanComponentInline]


class OfferingAdminForm(ModelForm):
    class Meta:
        widgets = {
            'attributes': JSONEditor(),
            'geolocations': JSONEditor(),
            'options': JSONEditor(),
        }


class OfferingComponentInline(admin.TabularInline):
    model = models.OfferingComponent


def get_admin_url_for_scope(scope):
    if isinstance(scope, ServiceSettings):
        model = scope.shared and SharedServiceSettings or PrivateServiceSettings
    else:
        model = scope
    return reverse('admin:%s_%s_change' % (scope._meta.app_label, model._meta.model_name), args=[scope.id])


def get_admin_link_for_scope(scope):
    return format_html('<a href="{}">{}</a>', get_admin_url_for_scope(scope), scope)


class OfferingAdmin(admin.ModelAdmin):
    form = OfferingAdminForm
    inlines = [ScreenshotsInline, PlansInline, OfferingComponentInline]
    list_display = ('name', 'customer', 'state', 'category')
    list_filter = ('state', 'shared', ('category', RelatedOnlyDropdownFilter),)
    search_fields = ('name', 'uuid')
    fields = ('state', 'customer', 'category', 'name', 'native_name',
              'description', 'native_description', 'full_description',
              'rating', 'thumbnail', 'attributes', 'options', 'geolocations',
              'shared', 'allowed_customers', 'type', 'scope_link', 'vendor_details')
    readonly_fields = ('rating', 'scope_link')

    def scope_link(self, obj):
        if obj.scope:
            return format_html('<a href="{}">{}</a>', get_admin_url_for_scope(obj.scope), obj.scope)

    actions = ['activate']

    def activate(self, request, queryset):
        valid_states = [models.Offering.States.DRAFT, models.Offering.States.PAUSED]
        valid_offerings = queryset.filter(state__in=valid_states)
        count = valid_offerings.count()

        for offering in valid_offerings:
            offering.activate()
            offering.save()

        message = ungettext(
            'One offering has been activated.',
            '%(count)d offerings have been activated.',
            count
        )
        message = message % {'count': count}

        self.message_user(request, message)

    activate.short_description = _('Activate offerings')


class OrderItemInline(admin.TabularInline):
    model = models.OrderItem
    fields = ('offering', 'state', 'attributes', 'cost', 'plan')
    readonly_fields = fields


class OrderAdmin(core_admin.ExtraActionsMixin, admin.ModelAdmin):
    list_display = ('uuid', 'project', 'created', 'created_by', 'state', 'total_cost')
    list_filter = ('state', 'created')
    ordering = ('-created',)
    inlines = [OrderItemInline]

    def get_extra_actions(self):
        return [
            self.create_pdf_for_all,
        ]

    def create_pdf_for_all(self, request):
        tasks.create_pdf_for_all.delay()
        message = _('PDF creation has been scheduled')
        self.message_user(request, message)
        return redirect(reverse('admin:marketplace_order_changelist'))

    create_pdf_for_all.name = _('Create PDF for all orders')


class ResourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'state', 'category', 'created')
    list_filter = (
        'state',
        ('project', RelatedOnlyDropdownFilter),
        ('offering', RelatedOnlyDropdownFilter),
    )
    readonly_fields = fields = ('state', 'scope_link', 'project_link', 'offering_link',
                                'plan_link', 'formatted_attributes', 'formatted_limits')
    search_fields = ('name', 'uuid')

    def category(self, obj):
        return obj.offering.category

    def scope_link(self, obj):
        return get_admin_link_for_scope(obj.scope)

    scope_link.short_description = 'Scope'

    def project_link(self, obj):
        return get_admin_link_for_scope(obj.project)

    project_link.short_description = 'Project'

    def offering_link(self, obj):
        return get_admin_link_for_scope(obj.offering)

    offering_link.short_description = 'Offering'

    def plan_link(self, obj):
        return get_admin_link_for_scope(obj.plan)

    plan_link.short_description = 'Plan'

    def formatted_attributes(self, obj):
        return format_json_field(obj.attributes)

    formatted_attributes.allow_tags = True
    formatted_attributes.short_description = 'Attributes'

    def formatted_limits(self, obj):
        return format_json_field(obj.limits)

    formatted_limits.allow_tags = True
    formatted_limits.short_description = 'Limits'


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Section, SectionAdmin)
admin.site.register(models.Attribute, AttributeAdmin)
admin.site.register(models.Screenshot)
admin.site.register(models.Order, OrderAdmin)
admin.site.register(models.Plan, PlanAdmin)
admin.site.register(models.Resource, ResourceAdmin)
