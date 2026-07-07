"""
base.py — shared scaffolding for all cloud cleaners.

Each concrete cleaner (AWSCleaner, AzureCleaner, GCPCleaner, ...) subclasses
BaseCleaner and sets TOTAL, implements __init__ (client setup) and run().
"""

from job_store import update_job, step_record


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

    def _finalize(self, number: int, name: str, details: list):
        """Emit terminal state for a step — 'error' if any detail starts with
        'error', else 'done'. Prevents a step from silently succeeding when
        a delete helper swallowed an exception into a detail string."""
        msg = "; ".join(details) or "none found"
        state = "error" if any(d.startswith("error") for d in details) else "done"
        self._emit(number, name, state, msg)

    def run(self):
        raise NotImplementedError
