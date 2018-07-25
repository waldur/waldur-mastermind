from __future__ import unicode_literals

from waldur_core.users import views


def register_in(router):
    router.register(r'user-invitations', views.InvitationViewSet, base_name='user-invitation')
