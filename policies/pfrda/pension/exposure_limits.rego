# data.pfrda.pension.circulars.pfrda_2024_12_sup_cir_07.clause_3
#
# METADATA
# title: NPS Equity Exposure Limit Rule
# description: Pension Fund Manager must not exceed 75% equity exposure for an Active Choice NPS subscriber under age 50.
# custom:
#   circular_number: PFRDA/2024/12/SUP-CIR/07
#   clause_number: "3"
#   regulator: pfrda
#   domain: pension
package pfrda.pension.circulars.pfrda_2024_12_sup_cir_07.clause_3

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"PensionFundManager"}
}

cond_0 if {
	input.facts.equity_exposure_pct <= 75
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.equity_exposure_pct > 75
	msg := sprintf(
		"Equity Exposure is %v %%, which fails the required condition (<= 75 %%, clause 3)",
		[input.facts.equity_exposure_pct],
	)
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "pfrda_2024_12_sup_cir_07:3",
}
