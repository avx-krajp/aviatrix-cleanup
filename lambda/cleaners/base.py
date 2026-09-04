"""
base.py — shared scaffolding for all cloud cleaners.

Each concrete cleaner (AWSCleaner, AzureCleaner, GCPCleaner, ...) subclasses
BaseCleaner and sets TOTAL, implements __init__ (client setup) and run().
"""

from job_store import update_job, step_record

# Resources (or the VPC/resource-group/network that contains them) carrying
# this tag/label are never torn down, regardless of any "aviatrix" match that
# would otherwise select them for deletion. Key and value are matched
# case-insensitively so "Csp-Cost-Ignore: Yes" etc. also count.
CSP_IGNORE_KEY = "csp-cost-ignore"
CSP_IGNORE_VALUE = "yes"


class BaseCleaner:
    TOTAL = 0

    def __init__(self, table, job_id: str, dry_run: bool):
        self.table   = table
        self.job_id  = job_id
        self.dry_run = dry_run
        self.any_error = False

    def _emit(self, number: int, name: str, state: str, detail: str = ""):
        if state == "error":
            self.any_error = True
        update_job(self.table, self.job_id, "RUNNING",
                   step_record(number, self.TOTAL, name, state, detail))

    @staticmethod
    def _is_ignore_tag(tags) -> bool:
        """tags may be a dict (Azure .tags, GCP labels) or a list of AWS-style
        {'Key':..,'Value':..} dicts (or None). Returns True if any key
        matches CSP_IGNORE_KEY (case-insensitive) with value CSP_IGNORE_VALUE
        (case-insensitive)."""
        if not tags:
            return False
        if isinstance(tags, dict):
            items = tags.items()
        else:
            items = ((t.get("Key"), t.get("Value")) for t in tags)
        for k, v in items:
            if (k or "").strip().lower() == CSP_IGNORE_KEY \
                    and (v or "").strip().lower() == CSP_IGNORE_VALUE:
                return True
        return False

    def _finalize(self, number: int, name: str, details: list):
        """Emit terminal state for a step — 'error' if any detail starts with
        'error', else 'done'. Prevents a step from silently succeeding when
        a delete helper swallowed an exception into a detail string."""
        msg = "; ".join(details) or "none found"
        state = "error" if any(d.startswith("error") for d in details) else "done"
        self._emit(number, name, state, msg)

    def run(self):
        raise NotImplementedError
