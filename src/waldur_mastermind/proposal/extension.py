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
            "proposals-for-ended-rounds-should-be-cancelled": {
                "task": "waldur_mastermind.proposal.proposals_for_ended_rounds_should_be_cancelled",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "expired-reviews-should-be-cancelled": {
                "task": "waldur_mastermind.proposal.expired_reviews_should_be_cancelled",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "notify_reviewer_on_round_start": {
                "task": "waldur_mastermind.proposal.notify_reviewer_on_round_start",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "notify_manager_on_round_cutoff": {
                "task": "waldur_mastermind.proposal.notify_manager_on_round_cutoff",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "notify-proposal-creator-on-submission-deadline-approaching": {
                "task": "waldur_mastermind.proposal.notify_proposal_creator_on_submission_deadline_approaching",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "notify-reviewer-on-review-deadline-approaching": {
                "task": "waldur_mastermind.proposal.notify_reviewer_on_review_deadline_approaching",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "mark-expired-assignment-batches": {
                "task": "waldur_mastermind.proposal.mark_expired_assignment_batches",
                "schedule": timedelta(minutes=15),
                "args": (),
            },
            "mark-expired-workflow-steps": {
                "task": "waldur_mastermind.proposal.mark_expired_workflow_steps",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "send-workflow-step-deadline-reminders": {
                "task": "waldur_mastermind.proposal.send_workflow_step_deadline_reminders",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "send-assignment-expiry-reminders": {
                "task": "waldur_mastermind.proposal.send_assignment_expiry_reminders",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "notify-managers-of-expired-batches": {
                "task": "waldur_mastermind.proposal.notify_managers_of_expired_batches",
                "schedule": timedelta(minutes=30),
                "args": (),
            },
        }
