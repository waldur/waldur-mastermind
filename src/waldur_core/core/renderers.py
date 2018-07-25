from rest_framework import renderers

from waldur_core import __version__


class BrowsableAPIRenderer(renderers.BrowsableAPIRenderer):
    """
    HTML renderer used to self-document the API.

    This renderer populates version of the server running
    to the context.
    """

    def get_context(self, data, accepted_media_type, renderer_context):
        context = super(BrowsableAPIRenderer, self).get_context(data, accepted_media_type, renderer_context)
        context['version'] = __version__
        return context
