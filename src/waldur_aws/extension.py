from waldur_core.core import WaldurExtension


class AWSExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_aws"

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def get_cleanup_executor():
        from .executors import AWSCleanupExecutor

        return AWSCleanupExecutor
