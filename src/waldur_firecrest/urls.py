from . import views


def register_in(router):
    router.register(r"slurm-jobs", views.FirecrestJobViewSet, basename="slurm-job")
