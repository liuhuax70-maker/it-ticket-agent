{{/* 公共命名与标签助手 */}}

{{- define "rag-service.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rag-service.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "rag-service.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rag-service.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "rag-service.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: permission-aware-rag
{{- end -}}

{{- define "rag-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rag-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "rag-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "rag-service.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
