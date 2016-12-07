from __future__ import unicode_literals

from nodeconductor_assembly_waldur.support import views


def register_in(router):
    router.register(r'waldur-issues', views.IssueViewSet, base_name='waldur-issues')
