# Compiler Service

Compiles audited, extracted compliance rules into OPA Rego policy
modules (and a JSON-Logic fallback AST for non-OPA consumers).

Run locally: `uvicorn app.main:app --reload --port 8003`
