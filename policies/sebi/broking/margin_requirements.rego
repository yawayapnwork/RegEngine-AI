# data.sebi.broking.circulars.sebi_ho_mirsd_dop_cir_p_2024_100.clause_3_2
#
# Example of the exact shape app.compiler.rego_compiler.compile_rule_to_rego
# generates automatically -- hand-maintained here only to document the
# namespace convention (see policies/README.md); real compiled output is
# never hand-edited.
#
# METADATA
# title: Upfront Margin Compliance Rule
# description: Stockbroker must collect upfront margin of at least 20% of transaction value.
# custom:
#   circular_number: SEBI/HO/MIRSD/DOP/CIR/P/2024/100
#   clause_number: "3.2"
#   regulator: sebi
#   domain: broking
package sebi.broking.circulars.sebi_ho_mirsd_dop_cir_p_2024_100.clause_3_2

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"Stockbroker"}
}

cond_0 if {
	input.facts.upfront_margin_pct >= 20
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.upfront_margin_pct < 20
	msg := sprintf("Upfront Margin is %v %%, which fails the required condition (>= 20 %%, clause 3.2)", [input.facts.upfront_margin_pct])
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "sebi_ho_mirsd_dop_cir_p_2024_100:3.2",
	"circular_number": "SEBI/HO/MIRSD/DOP/CIR/P/2024/100",
	"clause_number": "3.2",
}
