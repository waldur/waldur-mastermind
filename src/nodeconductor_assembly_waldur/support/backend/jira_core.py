from django.conf import settings
from nodeconductor_assembly_waldur.support.backend.base import AtlassianBackend


class JiraBackend(AtlassianBackend):

    def _get_credentials(self):
        return settings.WALDUR_SUPPORT.get('CREDENTIALS', {})

    def _get_project_details(self):
        return settings.WALDUR_SUPPORT.get('PROJECT', {})