from django.apps import AppConfig


class AnsibleEstimatorConfig(AppConfig):
    name = 'waldur_mastermind.ansible_estimator'
    verbose_name = 'Ansible estimator'

    def ready(self):
        pass
