# data.sebi.amc.circulars.sebi_ho_imd_2024_55.clause_5_1
#
# METADATA
# title: Scheme Disclosure Timeline Rule
# description: AMC must publish scheme portfolio disclosure within 15 days of month-end.
# custom:
#   circular_number: SEBI/HO/IMD/2024/55
#   clause_number: "5.1"
#   regulator: sebi
#   domain: amc
package sebi.amc.circulars.sebi_ho_imd_2024_55.clause_5_1

import rego.v1

default allow := false

entity_matches if {
	input.entity_type in {"AssetManagementCompany", "MutualFund"}
}

cond_0 if {
	input.facts.disclosure_days <= 15
}

allow if {
	entity_matches
	cond_0
}

violation contains msg if {
	entity_matches
	input.facts.disclosure_days > 15
	msg := sprintf("Disclosure Days is %v days, which fails the required condition (<= 15 days, clause 5.1)", [input.facts.disclosure_days])
}

deny := violation

decision := {
	"allow": allow,
	"violations": violation,
	"rule_id": "sebi_ho_imd_2024_55:5.1",
}
