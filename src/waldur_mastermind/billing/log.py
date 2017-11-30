from waldur_core.logging.loggers import EventLogger, event_logger


class PriceEstimateEventLogger(EventLogger):

    class Meta:
        event_types = ('project_price_limit_updated', 'customer_price_limit_updated')
        event_groups = {
            'projects': ['project_price_limit_updated'],
            'customers': ['customer_price_limit_updated'],
        }


event_logger.register('price_estimate', PriceEstimateEventLogger)
