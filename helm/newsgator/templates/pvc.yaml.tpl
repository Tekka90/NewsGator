{{- if .Values.persistence.enabled }}
{{ $persistence := .Values.persistence }}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "newsgator.fullname" . }}-data
  labels:
    {{- include "newsgator.labels" . | nindent 4 }}
spec:
  accessModes:
    {{- range $persistence.accessModes }}
    - {{ . }}
    {{- end }}
  resources:
    requests:
      storage: {{ $persistence.size }}
  {{- if $persistence.storageClass }}
  storageClassName: {{ $persistence.storageClass }}
  {{- end }}
{{- end }}
