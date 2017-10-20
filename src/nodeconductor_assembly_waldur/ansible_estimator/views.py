from rest_framework import response, status, views

from . import estimator


class AnsibleEstimatorView(views.APIView):
    def post(self, request, format=None):
        report = estimator.get_report(request)
        return response.Response(report, status=status.HTTP_200_OK)
