from django import http


def cors_middleware(get_response):
    """
    If CORS preflight header, then create an empty body response (200 OK) and return it
    """

    def middleware(request):
        if request.method == "OPTIONS" and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META:
            response = http.HttpResponse()
            response["Content-Length"] = "0"
            return response

        return get_response(request)

    return middleware
