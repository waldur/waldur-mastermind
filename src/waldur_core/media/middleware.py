excluded_content_types = [
    "text/html",
    "text/css",
    "application/json",
    "application/javascript",
]


def attachment_middleware(get_response):
    def middleware(request):
        response = get_response(request)

        content_type = response.get("Content-Type", "").lower()

        if content_type and not (
            content_type.startswith(ctype) for ctype in excluded_content_types
        ):
            response["Content-Disposition"] = "attachment"

        return response

    return middleware
