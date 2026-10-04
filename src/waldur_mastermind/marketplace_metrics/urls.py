from django.urls import re_path

from . import views


def register_in(router):
    router.register(
        r"marketplace-metric-retention-policies",
        views.RetentionPolicyViewSet,
        basename="marketplace-metric-retention-policy",
    )
    router.register(
        r"marketplace-metric-definitions",
        views.MetricDefinitionViewSet,
        basename="marketplace-metric-definition",
    )
    router.register(
        r"marketplace-offering-metrics",
        views.OfferingMetricViewSet,
        basename="marketplace-offering-metric",
    )
    router.register(
        r"marketplace-metric-goals",
        views.MetricGoalViewSet,
        basename="marketplace-metric-goal",
    )
    router.register(
        r"marketplace-metric-points",
        views.MetricPointViewSet,
        basename="marketplace-metric-point",
    )


urlpatterns = [
    re_path(r"^api/otlp/v1/metrics/?$", views.OtlpMetricsView.as_view()),
    re_path(
        r"^api/marketplace-metric-series/$",
        views.MetricSeriesView.as_view(),
        name="marketplace-metric-series",
    ),
    re_path(
        r"^api/marketplace-metric-breakdown/$",
        views.MetricBreakdownView.as_view(),
        name="marketplace-metric-breakdown",
    ),
    re_path(
        r"^api/marketplace-project-metrics/$",
        views.ProjectMetricsView.as_view(),
        name="marketplace-project-metrics",
    ),
]
