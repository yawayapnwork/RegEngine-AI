{{/*
Chart name, truncated/sanitized for use in Kubernetes object names.
*/}}
{{- define "regengine-ai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, respecting fullnameOverride / nameOverride and
avoiding double-printing the release name into the chart name.
*/}}
{{- define "regengine-ai.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "regengine-ai.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels, applied to every object this chart creates.
*/}}
{{- define "regengine-ai.labels" -}}
helm.sh/chart: {{ include "regengine-ai.chart" . }}
{{ include "regengine-ai.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels -- kept minimal and stable; never add anything here that
could change between releases (Deployment.spec.selector is immutable).
*/}}
{{- define "regengine-ai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "regengine-ai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Per-role selector labels (api / worker / beat / migrate) -- lets each
Deployment/Job target only its own Pods despite sharing the chart-wide
selector labels above. Call as:
  include "regengine-ai.roleSelectorLabels" (dict "role" "api" "context" $)
("context" must be the full root context ($ or .) -- selectorLabels needs
.Values/.Release/.Chart, which a partial dict wouldn't carry.)
*/}}
{{- define "regengine-ai.roleSelectorLabels" -}}
{{ include "regengine-ai.selectorLabels" .context }}
app.kubernetes.io/component: {{ .role }}
{{- end }}

{{- define "regengine-ai.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "regengine-ai.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolves the image tag: explicit .Values.image.tag, else Chart.AppVersion.
*/}}
{{- define "regengine-ai.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}

{{/*
Name of the Secret every role reads env from -- either the chart-created
one or an operator-managed existingSecret (see values.yaml `secrets`).
*/}}
{{- define "regengine-ai.secretName" -}}
{{- if .Values.secrets.create }}
{{- include "regengine-ai.fullname" . }}
{{- else }}
{{- .Values.secrets.existingSecretName }}
{{- end }}
{{- end }}
