{{- define "demoapp.name" -}}
{{- printf "%s-demoapp" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "demoapp.selectorLabels" -}}
app.kubernetes.io/name: demoapp
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{- define "demoapp.labels" -}}
{{ include "demoapp.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
{{- define "demoapp.image" -}}
{{- $repo := required "Set image.repository to your registry/apps/demoapp" .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $repo .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (required "Set image.digest (recommended) or image.tag" .Values.image.tag) -}}
{{- end -}}
{{- end -}}
