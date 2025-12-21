import factory
from django.urls import reverse

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.structure.tests import factories as structure_factories

from .. import enums, models


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
            "checklists-admin-detail", kwargs={"uuid": checklist.uuid.hex}
        )

    @classmethod
    def get_admin_url(cls, checklist=None):
        if checklist is None:
            checklist = ChecklistFactory()
        return "http://testserver" + reverse(
            "checklists-admin-detail", kwargs={"uuid": checklist.uuid.hex}
        )

    @classmethod
    def get_admin_list_url(cls):
        return "http://testserver" + reverse("checklists-admin-list")


class QuestionFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Question]
):
    class Meta:
        model = models.Question

    description = factory.Sequence(lambda n: "question-%s" % n)
    checklist = factory.SubFactory(ChecklistFactory)
    order = factory.Sequence(int)
    question_type = enums.QuestionTypes.TEXT_INPUT
    operator = enums.OPERATORS[2][0]  # contains

    @classmethod
    def get_admin_url(cls, question=None, action=None):
        if question is None:
            question = QuestionFactory()
        url = "http://testserver" + reverse(
            "checklists-admin-questions-detail",
            kwargs={"uuid": question.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_admin_list_url(cls):
        return "http://testserver" + reverse("checklists-admin-questions-list")


class QuestionOptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.QuestionOption

    question = factory.SubFactory(QuestionFactory)
    label = factory.Sequence(lambda n: f"option-{n}")
    order = factory.Sequence(int)

    @classmethod
    def get_admin_url(cls, option=None, action=None):
        if option is None:
            option = QuestionOptionFactory()
        url = "http://testserver" + reverse(
            "checklists-admin-question-options-detail",
            kwargs={"uuid": option.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_admin_list_url(cls):
        return "http://testserver" + reverse("checklists-admin-question-options-list")


class QuestionDependencyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.QuestionDependency

    question = factory.SubFactory(QuestionFactory)
    depends_on_question = factory.SubFactory(QuestionFactory)
    required_answer_value = ["first", "second"]
    operator = enums.OPERATORS[2][0]

    @classmethod
    def get_admin_url(cls, dependency=None, action=None):
        if dependency is None:
            dependency = QuestionDependencyFactory()
        url = "http://testserver" + reverse(
            "checklists-admin-question-dependencies-detail",
            kwargs={"uuid": dependency.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_admin_list_url(cls):
        return "http://testserver" + reverse(
            "checklists-admin-question-dependencies-list"
        )


class AnswerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Answer

    user = factory.SubFactory(structure_factories.UserFactory)
    question = factory.SubFactory(QuestionFactory)
    answer_data = factory.Sequence(lambda n: f"answer-{n}")
