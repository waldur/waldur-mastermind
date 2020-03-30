from django.contrib import admin

from . import models


class CategoryAdmin(admin.ModelAdmin):
    fields = ('name', 'description')


class QuestionInline(admin.TabularInline):
    model = models.Question
    fields = ('order', 'description', 'solution', 'correct_answer', 'category')


class ChecklistAdmin(admin.ModelAdmin):
    inlines = [QuestionInline]
    list_display = ('name', 'description', 'category')
    list_filter = ('category',)
    fields = ('name', 'description', 'category')


admin.site.register(models.Checklist, ChecklistAdmin)
admin.site.register(models.Category, CategoryAdmin)
