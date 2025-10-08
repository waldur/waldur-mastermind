from dataclasses import dataclass


@dataclass
class HeappeConfig:
    heappe_url: str
    heappe_username: str
    heappe_password: str
    heappe_cluster_id: int
    heappe_local_base_path: str
    heappe_cluster_password: str | None = None
    heappe_project_id: str | None = None
    scratch_project_directory: str | None = None
    project_permanent_directory: str | None = None
