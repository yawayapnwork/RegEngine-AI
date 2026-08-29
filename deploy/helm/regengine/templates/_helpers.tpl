{{- define "regengine.fullname" -}}
{{ .Release.Name }}-{{ .Chart.Name }}
{{- end -}}

{{- define "regengine.labels" -}}
app.kubernetes.io/part-of: regengine
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
