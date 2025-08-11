from django.utils.functional import cached_property

from waldur_core.checklist.tests import (
    factories as checklist_factories,
)
from waldur_core.structure.tests import fixtures as structure_fixtures


class CheckListFixture(structure_fixtures.CustomerFixture):
    def __init__(self):
        self.question_option
        self.question_dependency

    @cached_property
    def checklist(self):
        return checklist_factories.ChecklistFactory(name="my_checklist")

    @cached_property
    def question(self):
        return checklist_factories.QuestionFactory(checklist=self.checklist)

    @cached_property
    def question_option(self):
        return checklist_factories.QuestionOptionFactory(question=self.question)

    @cached_property
    def question_dependency(self):
        return checklist_factories.QuestionDependencyFactory(
            depends_on_question=self.question,
            question=self.dependent_question,
        )

    @cached_property
    def dependent_question(self):
        return checklist_factories.QuestionFactory(checklist=self.checklist)
