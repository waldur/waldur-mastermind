from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r'^api/marketplace-checklists-categories/$',
        views.CategoriesView.as_view({'get': 'list'}),
    ),
    re_path(
        r'^api/marketplace-checklists-categories/(?P<uuid>[a-f0-9]+)/$',
        views.CategoriesView.as_view({'get': 'retrieve'}),
        name='marketplace-checklists-category-detail',
    ),
    re_path(
        r'^api/marketplace-checklists-categories/(?P<category_uuid>[a-f0-9]+)/checklists/$',
        views.CategoryChecklistsView.as_view({'get': 'list'}),
    ),
    re_path(
        r'^api/marketplace-checklists/$',
        views.ChecklistListView.as_view({'get': 'list'}),
        name='marketplace-checklist-list',
    ),
    re_path(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/$',
        views.ChecklistDetailView.as_view({'get': 'retrieve'}),
        name='marketplace-checklist-detail',
    ),
    re_path(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/questions/$',
        views.QuestionsView.as_view({'get': 'list'}),
    ),
    re_path(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/stats/$',
        views.StatsView.as_view(),
    ),
    re_path(
        r'^api/projects/(?P<project_uuid>[a-f0-9]+)/marketplace-checklists/$',
        views.ProjectStatsView.as_view(),
    ),
    re_path(
        r'^api/customers/(?P<customer_uuid>[a-f0-9]+)/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/$',
        views.CustomerStatsView.as_view(),
        name='marketplace-checklists-customer-stats',
    ),
    re_path(
        r'^api/customers/(?P<customer_uuid>[a-f0-9]+)/marketplace-checklists/$',
        views.CustomerChecklistUpdateView.as_view(),
        name='marketplace-checklists-customer',
    ),
    re_path(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/answers/$',
        views.AnswersListView.as_view({'get': 'list'}),
    ),
    re_path(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/answers/submit/$',
        views.AnswersSubmitView.as_view({'post': 'create'}),
    ),
    re_path(
        r'^api/marketplace-checklists/(?P<checklist_uuid>[a-f0-9]+)/user/(?P<user_uuid>[a-f0-9]+)/answers/$',
        views.UserAnswersListView.as_view({'get': 'list'}),
    ),
    re_path(
        r'^api/users/(?P<user_uuid>[a-f0-9]+)/marketplace-checklist-stats/$',
        views.UserStatsView.as_view(),
        name='marketplace-checklist-user-stats',
    ),
]
