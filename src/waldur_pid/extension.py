from waldur_core.core import WaldurExtension


class PIDExtension(WaldurExtension):

    class Settings:
        WALDUR_PID = {
            'DATACITE': {
                'REPOSITORY_ID': '',
                'PASSWORD': '',
                'PREFIX': '',
                'API_URL': 'https://example.com',
                'PUBLISHER': 'Waldur'
            }
        }

    @staticmethod
    def django_app():
        return 'waldur_pid'
