# data.rbi.lending.circulars.dor_cre_rec_no_45_2024_25.clause_4
#
# METADATA
# title: NBFC Single Borrower Exposure Limit
# description: NBFC exposure to a single borrower must not exceed 20% of Tier I capital.
# custom:
#   circular_number: DOR.CRE.REC.No.45/03.10.001/2024-25
#   clause_number: "4"
#   regulator: rbi
#   domain: lending
package rbi.lending.circulars.dor_cre_rec_no_45_2024_25.clause_4

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"NBFC"}
}

cond_0 if {
	input.facts.single_borrower_exposure_pct <= 20
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.single_borrower_exposure_pct > 20
	msg := sprintf(
		"Single Borrower Exposure is %v %% of Tier I capital, which fails the required condition (<= 20 %%, clause 4)",
		[input.facts.single_borrower_exposure_pct],
	)
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "dor_cre_rec_no_45_2024_25:4",
}
