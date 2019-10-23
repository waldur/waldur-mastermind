from . import views, models


def register_in(router):
    router.register(r'python-management', views.PythonManagementViewSet, basename=models.PythonManagement.get_url_name())
    router.register(r'pip-packages', views.PipPackagesViewSet, basename='pip_packages')
