import tablib
from import_export import fields, resources, widgets

from . import models

CategoryResource = resources.modelresource_factory(models.Category)
QuestionResource = resources.modelresource_factory(models.Question)


class ChecklistResource(resources.ModelResource):
    questions = fields.Field(column_name='questions', widget=widgets.JSONWidget(),)
    category_data = fields.Field(
        column_name='category_data', widget=widgets.JSONWidget()
    )

    def before_import_row(self, row, **kwargs):
        if row.get('category_data'):
            dataset = tablib.Dataset().load(row.get('category_data'), 'json')
            CategoryResource().import_data(dataset)

    def dehydrate_category_data(self, checklist):
        if checklist.category:
            dataset = CategoryResource().export(
                queryset=models.Category.objects.filter(pk=checklist.category.pk)
            )
            return dataset.json

    def dehydrate_questions(self, checklist):
        dataset = QuestionResource().export(queryset=checklist.questions.all())
        return dataset.json

    def save_m2m(self, instance, row, using_transactions, dry_run):
        super().save_m2m(instance, row, using_transactions, dry_run)

        if row.get('questions'):
            dataset = tablib.Dataset().load(row.get('questions'), 'json')
            result = QuestionResource().import_data(dataset)
            result

    class Meta:
        exclude = ('created', 'modified', 'uuid', 'customers')


ChecklistResource = resources.modelresource_factory(models.Checklist, ChecklistResource)
