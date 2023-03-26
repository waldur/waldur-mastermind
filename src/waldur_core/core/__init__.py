import inspect
import json
import os
from importlib.metadata import entry_points

# WALDUR_DISABLED_EXTENSIONS environment variable contains JSON-encoded list of strings
# each of which corresponds to Django Application name of corresponding Waldur extension.
# By default, if this variable is not defined, then all extensions are enabled.
# Usage example:
# WALDUR_DISABLED_EXTENSIONS=["waldur_auth_social"] waldur runserver
# Otherwise, only those extensions which are listed in WALDUR_EXTENSIONS are disabled.
# Please note however that waldur_freeipa is mandatory extension for the time being.
WALDUR_DISABLED_EXTENSIONS = os.environ.get('WALDUR_DISABLED_EXTENSIONS')

MANDATORY_EXTENSIONS = ["waldur_freeipa"]

if WALDUR_DISABLED_EXTENSIONS:
    try:
        WALDUR_DISABLED_EXTENSIONS = json.loads(WALDUR_DISABLED_EXTENSIONS)
    except ValueError:
        WALDUR_DISABLED_EXTENSIONS = None


class WaldurExtension:
    """Base class for Waldur extensions"""

    class Settings:
        """Defines extra django settings"""

        pass

    @staticmethod
    def update_settings(settings):
        pass

    @staticmethod
    def django_app():
        """Returns a django application name which will be added to INSTALLED_APPS"""
        raise NotImplementedError

    @staticmethod
    def django_urls():
        """Returns a list of django URL in urlpatterns format"""
        return []

    @staticmethod
    def rest_urls():
        """Returns a function which register URLs in REST API"""
        return lambda router: NotImplemented

    @staticmethod
    def celery_tasks():
        """Returns a dictionary with celery tasks which will be added to CELERY_BEAT_SCHEDULE"""
        return dict()

    @staticmethod
    def get_cleanup_executor():
        """Returns a Celery task to cleanup project resources"""
        pass

    @staticmethod
    def is_assembly():
        """Return True if plugin is assembly and should be installed last"""
        return False

    @classmethod
    def get_extensions(cls):
        for ext in cls._get_extensions():
            if (
                WALDUR_DISABLED_EXTENSIONS
                and ext.django_app() in WALDUR_DISABLED_EXTENSIONS
                and ext.django_app() not in MANDATORY_EXTENSIONS
            ):
                continue
            yield ext

    @classmethod
    def _get_extensions(cls):
        """Get a list of available extensions"""
        assemblies = []
        for waldur_extension in entry_points()['waldur_extensions']:
            extension_module = waldur_extension.load()
            if inspect.isclass(extension_module) and issubclass(extension_module, cls):
                if not extension_module.is_assembly():
                    yield extension_module
                else:
                    assemblies.append(extension_module)
        yield from assemblies

    @classmethod
    def is_installed(cls, extension):
        for ext in cls.get_extensions():
            if extension == ext.django_app():
                return True
        return False

    @staticmethod
    def get_public_settings():
        """Return extension public settings
        :return: list"""
        return []

    @staticmethod
    def get_dynamic_settings():
        """Return extension public dynamic settings
        :return: dict"""
        return {}
