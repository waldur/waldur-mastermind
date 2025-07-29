from drf_spectacular.generators import EndpointEnumerator, SchemaGenerator


class WaldurEndpointEnumerator(EndpointEnumerator):
    def get_allowed_methods(self, callback):
        disabled_actions = getattr(callback.cls, "disabled_actions", [])
        if hasattr(callback, "actions"):
            actions = {
                method
                for method, action in callback.actions.items()
                if action not in disabled_actions
            }
            if "http_method_names" in callback.initkwargs:
                http_method_names = set(callback.initkwargs["http_method_names"])
            else:
                http_method_names = set(callback.cls.http_method_names)

            methods = [method.upper() for method in actions & http_method_names]
        else:
            # pass to constructor allowed method names to get valid ones
            kwargs = {}
            if "http_method_names" in callback.initkwargs:
                kwargs["http_method_names"] = callback.initkwargs["http_method_names"]

            methods = callback.cls(**kwargs).allowed_methods

        return [
            method
            for method in methods
            if method not in ("OPTIONS", "TRACE", "CONNECT")
        ]


class WaldurSchemaGenerator(SchemaGenerator):
    endpoint_inspector_cls = WaldurEndpointEnumerator
