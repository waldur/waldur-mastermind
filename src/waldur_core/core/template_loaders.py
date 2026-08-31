from django.core.cache import cache
from django.template import Origin, TemplateDoesNotExist
from django.template.loaders.base import Loader as BaseLoader

from waldur_core.core.db_template_cache import get_cache_key, get_cache_notfound_key
from waldur_core.core.models import NotificationTemplate


class DatabaseTemplateLoader(BaseLoader):
    """
    Loads template content overridden in the database via NotificationTemplate.

    Tries the cache first, then NotificationTemplate.content, and falls through to
    the next configured loader (filesystem/app_directories) on a miss - caching
    that miss too, so an un-overridden template does not cost a DB query on every
    render.
    """

    def get_template_sources(self, template_name):
        yield Origin(name=template_name, template_name=template_name, loader=self)

    def get_contents(self, origin):
        template_name = origin.template_name
        cache_key = get_cache_key(template_name)
        cached_content = cache.get(cache_key)
        if cached_content is not None:
            return cached_content

        notfound_key = get_cache_notfound_key(template_name)
        if cache.get(notfound_key):
            raise TemplateDoesNotExist(template_name)

        try:
            template = NotificationTemplate.objects.get(path=template_name)
        except NotificationTemplate.DoesNotExist:
            template = None
        except NotificationTemplate.MultipleObjectsReturned:
            template = NotificationTemplate.objects.filter(path=template_name).first()

        if not template or not template.content:
            cache.set(notfound_key, True)
            raise TemplateDoesNotExist(template_name)

        cache.set(cache_key, template.content)
        return template.content
