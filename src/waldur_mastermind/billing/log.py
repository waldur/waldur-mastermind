from waldur_core.logging.loggers import EventLogger, event_logger
from waldur_core.structure.models import Project, Customer


class PriceEstimateEventLogger(EventLogger):

    class Meta:
        event_types = ('project_price_limit_updated', 'customer_price_limit_updated')
        event_groups = {
            'projects': ['project_price_limit_updated'],
            'customers': ['customer_price_limit_updated'],
        }

    @staticmethod
    def get_scopes(event_context):
        scope = event_context['scope']
        if isinstance(scope, Project):
            return {scope, scope.customer}
        elif isinstance(scope, Customer):
            return {scope}
        else:
            return set()


event_logger.register('price_estimate', PriceEstimateEventLogger)
