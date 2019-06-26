from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class VMwareExtension(WaldurExtension):

    class Settings:
        WALDUR_VMWARE = {
            'VM_DATASTORE': '',
            'VM_DATACENTER': '',
            'VM_FOLDER': '',
            'VM_RESOURCE_POOL': '',
        }

    @staticmethod
    def django_app():
        return 'waldur_vmware'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
