from __future__ import unicode_literals

from django.conf.urls import url

from . import views


def register_in(router):
    pass


urlpatterns = [
    url(r'^api/ansible-estimator/$', views.AnsibleEstimatorView.as_view(), name='ansible-estimator'),
]
