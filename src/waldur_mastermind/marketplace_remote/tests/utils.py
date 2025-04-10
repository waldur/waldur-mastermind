import json

import respx


def get_request_data(router: respx.Router):
    return json.loads(router.calls.last.request.content)


def get_query_params(router: respx.Router):
    return dict(router.calls.last.request.url.params)
