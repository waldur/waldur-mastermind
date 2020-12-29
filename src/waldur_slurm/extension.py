from waldur_core.core import WaldurExtension


class SlurmExtension(WaldurExtension):
    class Settings:
        WALDUR_SLURM = {
            'ENABLED': False,
            'CUSTOMER_PREFIX': 'waldur_customer_',
            'PROJECT_PREFIX': 'waldur_project_',
            'ALLOCATION_PREFIX': 'waldur_allocation_',
            'PRIVATE_KEY_PATH': '/etc/waldur/id_rsa',
            'DEFAULT_LIMITS': {
                'CPU': 16000,  # Measured unit is CPU-hours
                'GPU': 400,  # Measured unit is GPU-hours
                'RAM': 100000 * 2 ** 10,  # Measured unit is MB
            },
        }

    @staticmethod
    def django_app():
        return 'waldur_slurm'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def get_cleanup_executor():
        from waldur_slurm.executors import SlurmCleanupExecutor

        return SlurmCleanupExecutor
