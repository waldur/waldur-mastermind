from waldur_ansible.python_management import executors, models, utils

from . import locking_service


class InstalledLibrariesExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        self.persist_installed_libraries_in_db(
            request.python_management,
            request.virtual_env_name,
            lines_post_processor.installed_libraries_after_modifications)

    def persist_installed_libraries_in_db(self, python_management, virtual_environment_name, existing_packages):
        virtual_environment = utils.execute_safely(
            lambda: python_management.virtual_environments.get(name=virtual_environment_name))
        if virtual_environment and not existing_packages:
            virtual_environment.delete()
            return

        if not virtual_environment:
            virtual_environment = models.VirtualEnvironment(name=virtual_environment_name, python_management=python_management)
            virtual_environment.save()

        persisted_packages = virtual_environment.installed_libraries.all()

        self.save_newly_installed_libraries(
            existing_packages, persisted_packages, virtual_environment)

        self.delete_removed_libs(existing_packages, persisted_packages)

    def delete_removed_libs(self, existing_packages, persisted_packages):
        for persisted_package in persisted_packages:
            if not self.is_package_present(persisted_package, existing_packages):
                persisted_package.delete()

    def save_newly_installed_libraries(self, existing_packages, persisted_installed_libraries, virtual_environment):
        for installed_package in existing_packages:
            if not self.is_package_present(installed_package, persisted_installed_libraries):
                models.InstalledLibrary.objects.create(
                    name=installed_package.name, version=installed_package.version,
                    virtual_environment=virtual_environment)

    def is_package_present(self, package, packages_list):
        for p in packages_list:
            if package.name == p.name and package.version == p.version:
                return True
        return False


class PythonManagementDeletionRequestExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        request.python_management.delete()


class PythonManagementDeleteVirtualEnvExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        request.python_management.virtual_environments.get(name=request.virtual_env_name).delete()


class PythonManagementFindVirtualEnvsRequestExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        for persisted_virtual_env in request.python_management.virtual_environments.all():
            if persisted_virtual_env not in lines_post_processor.installed_virtual_environments:
                persisted_virtual_env.delete()

        locking_service.PythonManagementBackendLockingService.handle_on_processing_finished(request)

        for virtual_env_name in lines_post_processor.installed_virtual_environments:
            find_libs_request = models.PythonManagementFindInstalledLibrariesRequest.objects.create(
                python_management=request.python_management, virtual_env_name=virtual_env_name)
            executors.PythonManagementRequestExecutor.execute(find_libs_request, async=True)


class InitializationRequestExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        python_management = request.python_management
        python_management.python_version = lines_post_processor.python_version
        python_management.save()


class NullExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        pass
