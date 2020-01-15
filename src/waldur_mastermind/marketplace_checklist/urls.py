from . import views


def register_in(router):
    router.register(
        r'marketplace-checklist-questions',
        views.ChecklistViewset,
        basename='marketplace-checklist-question',
    )

    router.register(
        r'marketplace-checklist-answers',
        views.AnswerViewset,
        basename='marketplace-checklist-answer',
    )
