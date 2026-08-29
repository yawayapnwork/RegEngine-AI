# data.rbi.banking.circulars.rbi_2024_25_45.clause_2_a
#
# METADATA
# title: Capital to Risk-Weighted Assets Ratio (CRAR) Rule
# description: Scheduled commercial bank must maintain CRAR of at least 11.5%.
# custom:
#   circular_number: RBI/2024-25/45
#   clause_number: "2.a"
#   regulator: rbi
#   domain: banking
package rbi.banking.circulars.rbi_2024_25_45.clause_2_a

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"ScheduledCommercialBank", "SmallFinanceBank"}
}

cond_0 if {
	input.facts.crar_pct >= 11.5
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.crar_pct < 11.5
	msg := sprintf("CRAR is %v %%, which fails the required condition (>= 11.5 %%, clause 2.a)", [input.facts.crar_pct])
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "rbi_2024_25_45:2.a",
}
