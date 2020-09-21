from django.conf.urls import url

from . import views

urlpatterns = [
    url(
        r'^api/marketplace-checklists-categories/$',
        views.CategoriesView.as_view({'get': 'list'}),
    ),
    url(
        r'^api/marketplace-checklists-categories/(?P<uuid>[a-f0-9]+)/$',
        views.CategoriesView.as_view({'get': 'retrieve'}),
        name='marketplace-checklists-category-detail',
    ),
    url(
        r'^api/marketplace-checklists-categories/(?P<category_uuid>[a-f0-9]+)/checklists/$',
        views.CategoryChecklistsView.as_view({'get': 'list'}),
    ),
    url(r'^api/marketplace-checklists/$', views.ChecklistView.as_view({'get': 'list'})),
    url(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/questions/$',
        views.QuestionsView.as_view({'get': 'list'}),
    ),
    url(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/stats/$',
        views.StatsView.as_view(),
    ),
    url(
        r'^api/projects/(?P<project_uuid>[a-f0-9]+)/marketplace-checklists/$',
        views.ProjectStatsView.as_view(),
    ),
    url(
        r'^api/customers/(?P<customer_uuid>[a-f0-9]+)/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/$',
        views.CustomerStatsView.as_view(),
        name='marketplace-checklists-customer-stats',
    ),
    url(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/answers/$',
        views.AnswersListView.as_view({'get': 'list'}),
    ),
    url(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/answers/submit/$',
        views.AnswersSubmitView.as_view({'post': 'create'}),
    ),
    url(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/user/(?P<user_uuid>[a-f0-9]+)/answers/$',
        views.UserAnswersListView.as_view({'get': 'list'}),
    ),
]
