from . import views

from django.conf.urls import url

urlpatterns = [
    url(r'^api/marketplace-checklists/$',
        views.ChecklistView.as_view({'get': 'list'})),
    url(r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/questions/$',
        views.QuestionsView.as_view({'get': 'list'})),
    url(r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/stats/$',
        views.StatsView.as_view()),
    url(r'^api/projects/(?P<project_uuid>[a-f0-9]+)/marketplace-checklists/$',
        views.ProjectStatsView.as_view()),
    url(r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/answers/(?P<project_uuid>[a-z0-9]+)/$',
        views.AnswersListView.as_view({'get': 'list'})),
    url(r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/answers/(?P<project_uuid>[a-z0-9]+)/submit/$',
        views.AnswersSubmitView.as_view({'post': 'create'})),
]
