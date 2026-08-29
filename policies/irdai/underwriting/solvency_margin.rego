# data.irdai.underwriting.circulars.irdai_reg_gdl_017_2024.clause_6
#
# METADATA
# title: Insurer Solvency Margin Rule
# description: Insurer must maintain a solvency ratio of at least 1.5.
# custom:
#   circular_number: IRDAI/REG/GDL/017/2024
#   clause_number: "6"
#   regulator: irdai
#   domain: underwriting
package irdai.underwriting.circulars.irdai_reg_gdl_017_2024.clause_6

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"LifeInsurer", "GeneralInsurer", "HealthInsurer"}
}

cond_0 if {
	input.facts.solvency_ratio >= 1.5
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.solvency_ratio < 1.5
	msg := sprintf("Solvency Ratio is %v, which fails the required condition (>= 1.5, clause 6)", [input.facts.solvency_ratio])
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "irdai_reg_gdl_017_2024:6",
}
