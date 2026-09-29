class ReplayLeakage(RuntimeError):
    """Information from after the simulated moment reached (or was requested by) the decision layer. The run FAILS."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class ReplayIsolationError(RuntimeError):
    """The replay would touch production state (database, schema, ledger). Refused before anything is written."""
