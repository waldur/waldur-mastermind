from datetime import timedelta

from waldur_core.core import WaldurExtension


class ProposalExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.proposal"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        return {
            "create-reviews-if-strategy-is-after-round": {
                "task": "waldur_mastermind.proposal.create_reviews_if_strategy_is_after_round",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "create-reviews-if-strategy-is-after-proposal": {
                "task": "waldur_mastermind.proposal.create_reviews_if_strategy_is_after_proposal",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "proposals-for-ended-rounds-should-be-cancelled": {
                "task": "waldur_mastermind.proposal.proposals_for_ended_rounds_should_be_cancelled",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
