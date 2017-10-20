from django.apps import AppConfig


class AnsibleEstimatorConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.ansible_estimator'
    verbose_name = 'Ansible estimator'

    def ready(self):
        pass
