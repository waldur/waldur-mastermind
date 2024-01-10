from waldur_core.core import WaldurExtension


class PolicyExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.policy"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
