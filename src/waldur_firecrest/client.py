import requests


class FirecrestException(Exception):
    pass


class FirecrestClient:
    """
    Python client for Firecrest API
    https://firecrest.readthedocs.io/en/latest/reference.html
    """

    def __init__(self, api_url, access_token, machinename='cluster'):
        self.api_url = api_url
        self.headers = {
            'Authorization': f'Bearer {access_token}',
            'X-Machine-Name': machinename,
        }

    def _request(self, method, url, **kwargs):
        try:
            response = requests.request(
                method, self.api_url + url, headers=self.headers, **kwargs
            )
        except requests.exceptions.RequestException:
            raise FirecrestException('Unable to perform Firecrest API request.')
        if response.ok:
            return response.json()
        else:
            raise FirecrestException(
                f'Message: {response.reason}, status code: {response.status_code}'
            )

    def _get(self, url, **kwargs):
        return self._request('get', url, **kwargs)

    def _post(self, url, **kwargs):
        return self._request('post', url, **kwargs)

    def list_jobs(self, page_size=25, page_number=0):
        """
        Returns Firecrest task ID which fetches SLURM jobs.
        """
        return self._get(
            'compute/jobs', params={'pageSize': page_size, 'pageNumber': page_number}
        )['task_id']

    def get_task(self, task_id):
        """
        Returns Firecrest task details by its ID.
        """
        return self._get(f'tasks/{task_id}')['task']

    def submit_job(self, fileobject):
        return self._post('compute/jobs/upload', files={'file': fileobject})['task_id']
