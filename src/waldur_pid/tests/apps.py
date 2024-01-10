from django.apps import AppConfig


class TestConfig(AppConfig):
    name = "waldur_pid.tests"
    label = "pid_tests"
    service_name = "Test"

    def ready(self):
        pass
