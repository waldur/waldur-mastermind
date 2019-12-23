from waldur_core.core import WaldurExtension


class GeoIPExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_geo_ip'
