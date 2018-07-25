def build_additional_extra_args(python_management_request):
    return dict(
        virtual_env_name=python_management_request.virtual_env_name
    )


def build_sync_request_extra_args(synchronization_request):
    extra_vars = dict(
        libraries_to_install=synchronization_request.libraries_to_install,
        libraries_to_remove=synchronization_request.libraries_to_remove,
    )
    extra_vars.update(build_additional_extra_args(synchronization_request))
    return extra_vars
