from waldur_core.checklist import views


def register_in(router):
    router.register(
        r"checklists-admin",
        views.ChecklistAdminView,
        basename="checklists-admin",
    )
    router.register(
        r"checklists-admin-questions",
        views.QuestionsAdminView,
        basename="checklists-admin-questions",
    )
    router.register(
        r"checklists-admin-question-options",
        views.QuestionOptionAdminViewSet,
        basename="checklists-admin-question-options",
    )
    router.register(
        r"checklists-admin-question-dependencies",
        views.QuestionDependencyViewSet,
        basename="checklists-admin-question-dependencies",
    )
    router.register(
        r"checklists-admin-categories",
        views.CategoriesView,
        basename="checklists-admin-categories",
    )


urlpatterns = []
