from django.contrib import admin

from . import models


class QuestionInline(admin.TabularInline):
    model = models.Question
    fields = ('order', 'description', 'category')


class ChecklistAdmin(admin.ModelAdmin):
    inlines = [QuestionInline]
    list_display = ('name', 'description')
    fields = ('name', 'description')


admin.site.register(models.Checklist, ChecklistAdmin)
