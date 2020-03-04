import json

import pkg_resources

backend_node_response = json.loads(
    pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode()
)
