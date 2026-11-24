apiVersion: v1
kind: Secret
metadata:
  name: {{ include "newsgator.fullname" . }}
  labels:
    {{- include "newsgator.labels" . | nindent 4 }}
type: Opaque
data:
  DATABASE_URL: {{ required "config.databaseUrl must not be empty" .Values.config.databaseUrl | b64enc | quote }}
  SECRET_KEY: {{ .Values.config.secretKey | b64enc | quote }}
  LLM_API_KEY: {{ .Values.config.llm.apiKey | default "not-needed" | b64enc | quote }}
  {{- with .Values.config.vectorStore.qdrant.apiKey }}
  QDRANT_API_KEY: {{ . | b64enc | quote }}
  {{- end }}
  {{- with .Values.config.readeck.token }}
  READECK_TOKEN: {{ . | b64enc | quote }}
  {{- end }}
