from waldur_core.core import WaldurExtension


class CostPlanningExtension(WaldurExtension):
    class Settings:
        WALDUR_COST_PLANNING = {
            'currency': 'USD',
        }

    @staticmethod
    def django_app():
        return 'waldur_cost_planning'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
