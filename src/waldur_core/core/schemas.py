from rest_framework.schemas.generators import EndpointEnumerator


# XXX: Drop after removing HEAD requests
class WaldurEndpointInspector(EndpointEnumerator):
    def get_allowed_methods(self, callback):
        """
        Return a list of the valid HTTP methods for this endpoint.
        """
        if hasattr(callback, "actions"):
            return [
                method.upper() for method in callback.actions.keys() if method != "head"
            ]

        return [
            method
            for method in callback.cls().allowed_methods
            if method not in ("OPTIONS", "HEAD")
        ]
