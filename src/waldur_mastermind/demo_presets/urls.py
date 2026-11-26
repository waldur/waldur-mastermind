from django.urls import re_path

from waldur_mastermind.demo_presets import views


def register_in(router):
    router.register(
        r"marketplace-demo-presets",
        views.DemoPresetViewSet,
        basename="marketplace-demo-preset",
    )


urlpatterns = [
    re_path(
        r"^api/demo-preset-status/$",
        views.DemoPresetStatusView.as_view(),
        name="demo-preset-status",
    ),
]
