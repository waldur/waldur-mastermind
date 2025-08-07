from django.contrib import admin

from . import models


class QuestionOptionInline(admin.TabularInline):
    model = models.QuestionOption
    extra = 1
    fields = ("uuid", "label", "order")
    readonly_fields = ("uuid",)
    ordering = ("order",)


class QuestionAdmin(admin.ModelAdmin):
    inlines = [QuestionOptionInline]
    list_display = (
        "description",
        "checklist",
        "order",
        "question_type",
        "required",
        "always_requires_review",
    )
    list_filter = ("checklist", "question_type", "required", "always_requires_review")
    search_fields = ("description",)
    ordering = ("checklist", "order")


class CategoryAdmin(admin.ModelAdmin):
    fields = ("icon", "name", "description")


class ChecklistAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "category", "checklist_type", "uuid")
    list_filter = ("category", "checklist_type")
    fields = ("name", "description", "category", "checklist_type")


class QuestionDependencyAdmin(admin.ModelAdmin):
    list_display = (
        "question",
        "depends_on_question",
        "operator",
        "required_answer_value",
    )
    list_filter = ("operator",)
    search_fields = ("question__description", "depends_on_question__description")


class AnswerAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "question",
        "answer_data",
        "requires_review",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = ("question", "requires_review", "reviewed_by")
    search_fields = ("user__username", "question__description")


admin.site.register(models.Checklist, ChecklistAdmin)
admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Question, QuestionAdmin)
admin.site.register(models.QuestionOption)
admin.site.register(models.QuestionDependency, QuestionDependencyAdmin)
admin.site.register(models.Answer, AnswerAdmin)
