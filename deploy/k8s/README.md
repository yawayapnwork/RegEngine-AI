# Kubernetes Manifests

Raw manifests for environments not using the Helm chart in
`deploy/helm/regengine`. `namespace.yaml` creates the `regengine`
namespace every other manifest/the Helm release should target
(`helm install regengine deploy/helm/regengine -n regengine`).
