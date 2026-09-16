from django.urls import include, re_path

from waldur_core.core import WaldurExtension


class SramExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_sram"

    @staticmethod
    def django_urls():
        return [re_path(r"^scim/v2/sram/", include("waldur_sram.urls"))]
