from django.apps import AppConfig


class MediaConfig(AppConfig):
    name = 'waldur_core.media'
    verbose_name = 'Media'

    def ready(self):
        pass
