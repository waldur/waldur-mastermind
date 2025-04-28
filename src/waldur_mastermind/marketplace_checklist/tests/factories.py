import factory
from django.urls import reverse

from waldur_core.core.tests.types import BaseMetaFactory

from .. import models


class CategoryFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Category]
):
    class Meta:
        model = models.Category

    name = factory.Sequence(lambda n: "category-%s" % n)


class ChecklistFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Checklist]
):
    class Meta:
        model = models.Checklist

    name = factory.Sequence(lambda n: "checklist-%s" % n)
    category = factory.SubFactory(CategoryFactory)

    @classmethod
    def get_url(cls, checklist=None):
        if checklist is None:
            checklist = ChecklistFactory()
        return "http://testserver" + reverse(
            "marketplace-checklist-detail", kwargs={"uuid": checklist.uuid.hex}
        )


class QuestionFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Question]
):
    class Meta:
        model = models.Question

    checklist = factory.SubFactory(ChecklistFactory)
