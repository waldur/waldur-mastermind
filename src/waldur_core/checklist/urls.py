from django.urls import re_path

from . import views


def register_in(router):
    router.register(
        r"marketplace-checklists-admin",
        views.ChecklistAdminView,
        basename="marketplace-checklists-admin",
    )
    router.register(
        r"marketplace-checklists-admin-questions",
        views.QuestionsAdminView,
        basename="marketplace-checklists-admin-question",
    )
    router.register(
        r"marketplace-checklists-admin-question-options",
        views.QuestionOptionAdminViewSet,
        basename="marketplace-checklists-admin-question-option",
    )
    router.register(
        r"marketplace-checklists-admin-question-dependencies",
        views.QuestionDependencyViewSet,
        basename="marketplace-checklists-admin-question-dependency",
    )


urlpatterns = [
    re_path(
        r"^marketplace-checklists-categories/$",
        views.CategoriesView.as_view({"get": "list"}),
    ),
    re_path(
        r"^marketplace-checklists-categories/(?P<uuid>[a-f0-9]+)/$",
        views.CategoriesView.as_view({"get": "retrieve"}),
        name="marketplace-checklists-category-detail",
    ),
]
