from . import views


def register_in(router):
    router.register(
        prefix=r'marketplace-checklist-questions',
        viewset=views.ChecklistViewset,
        basename='marketplace-checklist-question',
    )

    router.register(
        prefix=r'marketplace-checklist-answers',
        viewset=views.AnswerViewset,
        basename='marketplace-checklist-answer',
    )
