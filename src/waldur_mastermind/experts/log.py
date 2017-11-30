from waldur_core.logging.loggers import EventLogger, event_logger

from . import models


class ExpertRequestEventLogger(EventLogger):
    expert_request = models.ExpertRequest

    class Meta:
        event_types = (
            'expert_request_created',
            'expert_request_activated',
            'expert_request_cancelled',
            'expert_request_completed',
        )
        event_groups = {
            'experts': event_types,
        }


class ExpertBidEventLogger(EventLogger):
    expert_bid = models.ExpertBid

    class Meta:
        event_types = ('expert_bid_created',)
        event_groups = {
            'experts': event_types,
        }


event_logger.register('waldur_expert_request', ExpertRequestEventLogger)
event_logger.register('waldur_expert_bid', ExpertBidEventLogger)
