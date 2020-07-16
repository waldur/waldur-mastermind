from django.contrib import admin
from import_export import admin as import_export_admin
from modeltranslation import admin as modeltranslation_admin

from . import models


class CategoryAdmin(import_export_admin.ImportExportModelAdmin):
    fields = ('name', 'description')


class QuestionInline(modeltranslation_admin.TranslationStackedInline):
    model = models.Question
    fields = ('order', 'description', 'solution', 'correct_answer', 'category')


class ChecklistAdmin(
    import_export_admin.ImportExportMixin, modeltranslation_admin.TranslationAdmin
):
    inlines = [QuestionInline]
    list_display = ('name', 'description', 'category')
    list_filter = ('category',)
    fields = ('name', 'description', 'category')


admin.site.register(models.Checklist, ChecklistAdmin)
admin.site.register(models.Category, CategoryAdmin)
