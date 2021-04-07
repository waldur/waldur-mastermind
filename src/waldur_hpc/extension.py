from waldur_core.core import WaldurExtension


class HPCExtension(WaldurExtension):
    class Settings:
        WALDUR_HPC = {
            'ENABLED': False,
            'INTERNAL_CUSTOMER_UUID': '',
            'EXTERNAL_CUSTOMER_UUID': '',
            'INTERNAL_AFFILIATIONS': [],
            'EXTERNAL_AFFILIATIONS': [],
            'INTERNAL_EMAIL_PATTERNS': [],
            'EXTERNAL_EMAIL_PATTERNS': [],
            'INTERNAL_LIMITS': {},
            'OFFERING_UUID': '',
            'PLAN_UUID': '',
        }

    @staticmethod
    def django_app():
        return 'waldur_hpc'
