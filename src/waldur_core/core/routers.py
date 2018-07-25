from collections import OrderedDict
from operator import itemgetter

from django.urls import NoReverseMatch
from rest_framework import views
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.routers import DefaultRouter


class SortedDefaultRouter(DefaultRouter):

    def get_api_root_view(self, api_urls=None):
        """
        Return a basic root view.
        """
        api_root_dict = OrderedDict()
        list_name = self.routes[0].name
        for prefix, viewset, basename in self.registry:
            api_root_dict[prefix] = list_name.format(basename=basename)

        class APIRootView(views.APIView):
            _ignore_model_permissions = True
            exclude_from_schema = True

            def get(self, request, *args, **kwargs):
                # Return a plain {"name": "hyperlink"} response.
                ret = OrderedDict()
                namespace = request.resolver_match.namespace
                for key, url_name in sorted(api_root_dict.items(), key=itemgetter(0)):
                    if namespace:
                        url_name = namespace + ':' + url_name
                    try:
                        ret[key] = reverse(
                            url_name,
                            args=args,
                            kwargs=kwargs,
                            request=request,
                            format=kwargs.get('format', None)
                        )
                    except NoReverseMatch:
                        # Don't bail out if eg. no list routes exist, only detail routes.
                        continue

                return Response(ret)

        return APIRootView.as_view()

    def get_default_base_name(self, viewset):
        """
        Attempt to automatically determine base name using `get_url_name`.
        """
        queryset = getattr(viewset, 'queryset', None)

        if queryset is not None:
            get_url_name = getattr(queryset.model, 'get_url_name', None)
            if get_url_name is not None:
                return get_url_name()

        return super(SortedDefaultRouter, self).get_default_base_name(viewset)

    def get_method_map(self, viewset, method_map):
        # head method is not included by default
        mappings = super(SortedDefaultRouter, self).get_method_map(viewset, method_map)
        if 'get' in mappings:
            mappings['head'] = mappings['get']

        return mappings
