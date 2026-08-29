# data.irdai.distribution.circulars.irdai_reg_gdl_022_2024.clause_9_1
#
# METADATA
# title: Insurance Intermediary Commission Cap Rule
# description: Commission paid to an insurance intermediary must not exceed 30% of first-year premium.
# custom:
#   circular_number: IRDAI/REG/GDL/022/2024
#   clause_number: "9.1"
#   regulator: irdai
#   domain: distribution
package irdai.distribution.circulars.irdai_reg_gdl_022_2024.clause_9_1

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"InsuranceIntermediary", "InsuranceBroker"}
}

cond_0 if {
	input.facts.first_year_commission_pct <= 30
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.first_year_commission_pct > 30
	msg := sprintf(
		"First Year Commission is %v %% of first-year premium, which fails the required condition (<= 30 %%, clause 9.1)",
		[input.facts.first_year_commission_pct],
	)
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "irdai_reg_gdl_022_2024:9.1",
}
