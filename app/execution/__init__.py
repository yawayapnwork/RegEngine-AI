"""Transaction execution service: evaluates broker/OMS/RMS transactions
against compiled RegEngine AI policies (via an embedded OPA engine),
routes ambiguous outcomes to HITL, and bridges legacy SFTP/CDC batch
pipelines and modern REST webhook consumers onto the same policy set
produced by `app.compiler`."""
