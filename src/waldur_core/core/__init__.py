import inspect

import pkg_resources

default_app_config = 'waldur_core.core.apps.CoreConfig'


class WaldurExtension(object):
    """ Base class for Waldur extensions """

    class Settings:
        """ Defines extra django settings """
        pass

    @staticmethod
    def update_settings(settings):
        pass

    @staticmethod
    def django_app():
        """ Returns a django application name which will be added to INSTALLED_APPS """
        raise NotImplementedError

    @staticmethod
    def django_urls():
        """ Returns a list of django URL in urlpatterns format """
        return []

    @staticmethod
    def rest_urls():
        """ Returns a function which register URLs in REST API """
        return lambda router: NotImplemented

    @staticmethod
    def celery_tasks():
        """ Returns a dictionary with celery tasks which will be added to CELERY_BEAT_SCHEDULE """
        return dict()

    @staticmethod
    def get_cleanup_executor():
        """ Returns a Celery task to cleanup project resources """
        pass

    @staticmethod
    def is_assembly():
        """ Return True if plugin is assembly and should be installed last """
        return False

    @classmethod
    def get_extensions(cls):
        """ Get a list of available extensions """
        assemblies = []
        for waldur_extension in pkg_resources.iter_entry_points('waldur_extensions'):
            extension_module = waldur_extension.load()
            if inspect.isclass(extension_module) and issubclass(extension_module, cls):
                if not extension_module.is_assembly():
                    yield extension_module
                else:
                    assemblies.append(extension_module)
        for assembly in assemblies:
            yield assembly

    @classmethod
    def is_installed(cls, extension):
        for ext in cls.get_extensions():
            if extension == ext.django_app():
                return True
        return False

    @staticmethod
    def get_public_settings():
        """ Return extension public settings
        :return: list """
        return []
