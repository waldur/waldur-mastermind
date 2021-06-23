from django.contrib import admin
from import_export import admin as import_export_admin
from modeltranslation import admin as modeltranslation_admin

from . import models
from .import_export_resources import ChecklistResource


class CategoryAdmin(import_export_admin.ImportExportModelAdmin):
    fields = ('icon', 'name', 'description')


class QuestionInline(modeltranslation_admin.TranslationStackedInline):
    model = models.Question
    fields = ('order', 'description', 'solution', 'correct_answer', 'category', 'image')


class ChecklistCustomerRoleInline(admin.StackedInline):
    model = models.ChecklistCustomerRole
    fields = ('role',)


class ChecklistProjectRoleInline(admin.StackedInline):
    model = models.ChecklistProjectRole
    fields = ('role',)


class ChecklistAdmin(
    import_export_admin.ImportExportMixin, modeltranslation_admin.TranslationAdmin
):
    inlines = [QuestionInline, ChecklistCustomerRoleInline, ChecklistProjectRoleInline]
    list_display = ('name', 'description', 'category', 'uuid')
    list_filter = ('category',)
    fields = ('name', 'description', 'category')

    resource_class = ChecklistResource


class AnswerAdmin(admin.ModelAdmin):
    list_display = ('user', 'question', 'value')
    list_filter = ('question',)


admin.site.register(models.Checklist, ChecklistAdmin)
admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Answer, AnswerAdmin)
