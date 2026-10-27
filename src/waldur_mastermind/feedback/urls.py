from . import views


def register_in(router):
    router.register(r"feedback-surveys", views.FeedbackSurveyViewSet)
    router.register(r"feedback-requests", views.FeedbackRequestViewSet)
