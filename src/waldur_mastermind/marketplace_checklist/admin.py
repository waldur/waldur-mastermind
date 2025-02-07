from django.contrib import admin
from modeltranslation import admin as modeltranslation_admin

from . import models


class CategoryAdmin(admin.ModelAdmin):
    fields = ("icon", "name", "description")


class QuestionInline(modeltranslation_admin.TranslationStackedInline):
    model = models.Question
    fields = ("order", "description", "solution", "correct_answer", "category", "image")


class ChecklistAdmin(modeltranslation_admin.TranslationAdmin):
    inlines = [QuestionInline]
    list_display = ("name", "description", "category", "uuid")
    list_filter = ("category",)
    fields = ("name", "description", "category", "roles")


class AnswerAdmin(admin.ModelAdmin):
    list_display = ("user", "question", "value")
    list_filter = ("question",)


admin.site.register(models.Checklist, ChecklistAdmin)
admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Answer, AnswerAdmin)
