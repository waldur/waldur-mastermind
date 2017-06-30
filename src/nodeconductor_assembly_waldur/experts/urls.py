from __future__ import unicode_literals

from nodeconductor_assembly_waldur.experts import views


def register_in(router):
    router.register(r'expert-providers', views.ExpertProviderViewSet, base_name='expertprovider')
