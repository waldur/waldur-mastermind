from . import views, models


def register_in(router):
    router.register(r'python-management', views.PythonManagementViewSet, base_name=models.PythonManagement.get_url_name())
    router.register(r'pip-packages', views.PipPackagesViewSet, base_name='pip_packages')
