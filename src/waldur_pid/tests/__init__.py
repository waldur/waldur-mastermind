from django.apps import AppConfig

default_app_config = 'waldur_pid.tests.TestConfig'


class TestConfig(AppConfig):
    name = 'waldur_pid.tests'
    label = 'pid_tests'
    service_name = 'Test'

    def ready(self):
        pass
