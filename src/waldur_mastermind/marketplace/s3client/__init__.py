"""A small S3 client for the bucket browser: SigV4 over requests, path-style."""

from .client import S3Client, S3ClientError, S3ConnectionError, S3TimeoutError

__all__ = ["S3Client", "S3ClientError", "S3ConnectionError", "S3TimeoutError"]
