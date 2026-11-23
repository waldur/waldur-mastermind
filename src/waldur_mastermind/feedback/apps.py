from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    name = "waldur_mastermind.feedback"
    verbose_name = "Feedback"

    def ready(self):
        from waldur_core.checklist.utils import CHECKLIST_EDIT_GUARDS

        from .utils import refuse_prompted_survey_checklist

        CHECKLIST_EDIT_GUARDS.append(refuse_prompted_survey_checklist)
