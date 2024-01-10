from rest_framework import status, views
from rest_framework.response import Response

from waldur_core.core.utils import get_lat_lon_from_address

from . import serializers


class GeocodeViewSet(views.APIView):
    def get(self, request):
        serializer = serializers.GeoCodeSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        address = serializer.validated_data["address"]
        lat_lon = get_lat_lon_from_address(address)
        if lat_lon:
            return Response(
                {"latitude": lat_lon[0], "longitude": lat_lon[1]},
                status=status.HTTP_200_OK,
            )
        return Response(None, status=status.HTTP_200_OK)
