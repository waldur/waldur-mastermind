from __future__ import unicode_literals

from . import views


def register_in(router):
    router.register(r'freeipa-profiles', views.ProfileViewSet, base_name='freeipa-profile')
