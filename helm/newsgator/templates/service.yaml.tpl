apiVersion: v1
kind: Service
metadata:
  name: {{ include "newsgator.fullname" . }}
  labels:
    {{- include "newsgator.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: http-backend
      protocol: TCP
      name: http-backend
    - port: {{ .Values.serviceFrontend.port }}
      targetPort: http-frontend
      protocol: TCP
      name: http-frontend
  selector:
    {{- include "newsgator.selectorLabels" . | nindent 4 }}
