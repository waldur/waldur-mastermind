import factory

from .. import models


class ChecklistFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Checklist

    name = factory.Sequence(lambda n: 'checklist-%s' % n)


class QuestionFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Question

    checklist = factory.SubFactory(ChecklistFactory)
