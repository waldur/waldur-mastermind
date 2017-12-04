from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class AnsibleEstimatorExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.ansible_estimator'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns

    @staticmethod
    def is_assembly():
        return True
