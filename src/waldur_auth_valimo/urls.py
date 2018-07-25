from __future__ import unicode_literals

from . import views


def register_in(router):
    router.register(r'auth-valimo', views.AuthResultViewSet, base_name='auth-valimo')
