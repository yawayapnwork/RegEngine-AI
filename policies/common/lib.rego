# Shared helpers importable by any regulator/domain package as
# `import data.common`. Kept deliberately tiny and generic -- anything
# regulator-specific belongs in that regulator's own domain package, not
# here, so this file never becomes a dumping ground that recreates
# cross-namespace coupling the directory layout is meant to avoid.
package common

import rego.v1

# True when `input.evaluated_at` (RFC3339) falls on or after `since`
# (RFC3339) -- used by clauses with a "with effect from <date>" proviso,
# which appear across every regulator's circulars/directions.
effective_since(since) if {
	time.parse_rfc3339_ns(input.evaluated_at) >= time.parse_rfc3339_ns(since)
}

# INR crore -> INR, for clauses that state a threshold in crore but the
# `input.facts` value arrives in absolute rupees (app.compiler.naming's
# metric_field_name suffixes the field with the ORIGINAL unit, so this
# conversion is only needed when a caller's fact source uses a different
# unit than the clause was authored in).
crore_to_inr(crore) := crore * 10000000
