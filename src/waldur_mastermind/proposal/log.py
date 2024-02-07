from waldur_core.logging.loggers import EventLogger, event_logger


class CallProposalLogger(EventLogger):
    class Meta:
        event_types = (
            "call_proposal_document_added",
            "call_proposal_document_removed",
        )
        event_groups = {
            "proposal": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        print(event_context)
        return {event_context["customer"]}


event_logger.register("proposal", CallProposalLogger)
