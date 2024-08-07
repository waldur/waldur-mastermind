from waldur_core.logging.loggers import EventLogger, event_logger
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.proposal.models import Call, Proposal, Review


class ProposalLogger(EventLogger):
    proposal = Proposal

    class Meta:
        event_types = (
            "proposal_document_added",
            "proposal_document_removed",
            "proposal_canceled",
        )
        event_groups = {
            "proposal": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {_get_customer(event_context["proposal"])}


class CallLogger(EventLogger):
    call = Call

    class Meta:
        event_types = (
            "call_document_added",
            "call_document_removed",
        )
        event_groups = {
            "call": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {_get_customer(event_context["call"])}


class ReviewLogger(EventLogger):
    review = Review

    class Meta:
        event_types = ("review_canceled",)
        event_groups = {
            "review": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {_get_customer(event_context["review"])}


event_logger.register("proposal", ProposalLogger)
event_logger.register("call", CallLogger)
event_logger.register("review", ReviewLogger)
