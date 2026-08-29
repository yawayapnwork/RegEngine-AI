# Multi-Regulator OPA Policy Layout

Every compiled rule lives under `data.<regulator>.<domain>.circulars.<circular_slug>.clause_<clause_slug>`,
matching `app.compiler.naming.rego_package_name` and `app.regulatory.taxonomy`:

```
policies/
  common/                     # shared helper rules, imported by every regulator/domain package
    lib.rego
  sebi/
    broking/                  # data.sebi.broking.*        -- stockbrokers
      margin_requirements.rego
    amc/                      # data.sebi.amc.*            -- AMCs / mutual funds
      disclosure_requirements.rego
  rbi/
    banking/                  # data.rbi.banking.*         -- scheduled commercial banks
      capital_adequacy.rego
    lending/                  # data.rbi.lending.*         -- NBFC lending norms
      nbfc_exposure_limits.rego
  irdai/
    underwriting/             # data.irdai.underwriting.*  -- insurers
      solvency_margin.rego
    distribution/             # data.irdai.distribution.*  -- intermediaries/brokers
      commission_caps.rego
  pfrda/
    pension/                  # data.pfrda.pension.*       -- NPS pension fund managers
      exposure_limits.rego
```

Every `clause_*.rego` file here is a HAND-WRITTEN EXAMPLE of the shape
`app.compiler.rego_compiler.compile_rule_to_rego` generates automatically
from an `ExtractedComplianceRule` -- they exist to document the namespace
convention and give OPA bundle tooling something concrete to build/test
against, not as the primary way rules get authored. Real compiled rules
are written by the compiler at `app.execution.opa_engine.OPAEngine.publish_policy`
time and never hand-edited.

## Namespace isolation, not just naming

Two properties fall out of this layout, both load-bearing:

1. **A broker's compliance decision can never accidentally evaluate an
   RBI banking rule, or vice versa** -- `app.execution.evaluator.Evaluator`
   only ever queries `data.<package>.decision` for packages returned by
   the tenant's own `policy_registry.policies_for(entity_type)` lookup
   (app.execution.policy_cache), and those packages are namespaced by
   regulator+domain at compile time. There is no cross-namespace `import`
   between regulator packages -- only `common/lib.rego`'s helpers are
   shared, and only via explicit `import data.common`.

2. **Per-tenant risk overlays stay a separate axis.** A tenant's custom
   overlay policies live under `data.tenants.<tenant_id>.*`
   (`Tenant.opa_bundle_prefix`, see app.db.models) -- completely
   orthogonal to the `<regulator>.<domain>` axis here. A stockbroker
   tenant's overlay can reference `data.sebi.broking.*` facts but never
   needs to know RBI/IRDAI/PFRDA packages exist at all.

## OPA bundle structure

Each regulator subtree is built as its own OPA bundle (`opa build -b
policies/sebi -o sebi-bundle.tar.gz`), so a deployment that only serves
SEBI-regulated brokers never loads RBI/IRDAI/PFRDA rules into its OPA
process at all -- bundle size and policy-evaluation surface area both
scale with which regulators a given deployment actually needs, not with
how many this platform supports overall.
