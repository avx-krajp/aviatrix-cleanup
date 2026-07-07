"""
cleaners — one module per cloud provider. Adding a new cloud provider means
writing lambda/cleaners/<newcloud>.py with a BaseCleaner subclass and adding
one entry to CLEANER_REGISTRY below — no changes needed to cleanup_worker.py.
"""

from .aws import AWSCleaner
from .azure import AzureCleaner
from .gcp import GCPCleaner

CLEANER_REGISTRY = {
    "aws":   AWSCleaner,
    "azure": AzureCleaner,
    "gcp":   GCPCleaner,
}
