from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import get_doc


class WaldurOpenApiInspector(AutoSchema):
    def get_description(self) -> str:
        action_or_method = getattr(
            self.view, getattr(self.view, "action", self.method.lower()), None
        )
        return get_doc(action_or_method)

    def get_operation_id(self) -> str:
        path = self._tokenize_path()

        use_short = (
            getattr(self.view, "detail", False)
            or getattr(self.view, "action", "") != "create"
        ) and self.method == "POST"

        if not use_short:
            if self.method == "GET" and self._is_list_view():
                path.append("list")
            else:
                path.append(self.method_mapping[self.method.lower()])

        return "_".join([t.replace("-", "_") for t in path])
