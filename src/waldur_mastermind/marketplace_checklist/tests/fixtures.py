from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace_checklist.tests import (
    factories as marketplace_checklist_factories,
)


class CheckListFixture(structure_fixtures.CustomerFixture):
    def __init__(self):
        self.question_option
        self.question_dependency

    @cached_property
    def checklist(self):
        return marketplace_checklist_factories.ChecklistFactory(name="my_checklist")

    @cached_property
    def question(self):
        return marketplace_checklist_factories.QuestionFactory(checklist=self.checklist)

    @cached_property
    def question_option(self):
        return marketplace_checklist_factories.QuestionOptionFactory(
            question=self.question
        )

    @cached_property
    def question_dependency(self):
        return marketplace_checklist_factories.QuestionDependencyFactory(
            depends_on_question=self.question,
            question=self.dependent_question,
        )

    @cached_property
    def dependent_question(self):
        return marketplace_checklist_factories.QuestionFactory(checklist=self.checklist)
