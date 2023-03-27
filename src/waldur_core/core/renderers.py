from django.utils.encoding import smart_text
from rest_framework import renderers

from waldur_core import __version__


class BrowsableAPIRenderer(renderers.BrowsableAPIRenderer):
    """
    HTML renderer used to self-document the API.

    This renderer populates version of the server running
    to the context.
    """

    def get_context(self, data, accepted_media_type, renderer_context):
        context = super().get_context(data, accepted_media_type, renderer_context)
        context['version'] = __version__
        return context


class PlainTextRenderer(renderers.BaseRenderer):
    media_type = 'text/plain'
    format = 'txt'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return smart_text(data, encoding=self.charset)
