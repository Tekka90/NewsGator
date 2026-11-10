apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "newsgator.fullname" . }}
  labels:
    {{- include "newsgator.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "newsgator.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      annotations:
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml.tpl") . | sha256sum }}
        checksum/secret: {{ include (print $.Template.BasePath "/secret.yaml.tpl") . | sha256sum }}
        {{- with .Values.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      labels:
        {{- include "newsgator.labels" . | nindent 8 }}
        {{- with .Values.podLabels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "newsgator.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ .Chart.Name }}
          securityContext:
            {{- toYaml .Values.securityContext | nindent 12 }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          envFrom:
            - configMapRef:
                name: {{ include "newsgator.fullname" . }}
            - secretRef:
                name: {{ include "newsgator.fullname" . }}
          ports:
            - name: http-backend
              containerPort: {{ .Values.service.port }}
              protocol: TCP
            - name: http-frontend
              containerPort: {{ .Values.serviceFrontend.port }}
              protocol: TCP
          {{- if .Values.startupProbe.enabled }}
          startupProbe:
            {{- toYaml (omit .Values.startupProbe "enabled") | nindent 12 }}
          {{- end }}
          {{- if .Values.livenessProbe.enabled }}
          livenessProbe:
            {{- toYaml (omit .Values.livenessProbe "enabled") | nindent 12 }}
          {{- end }}
          {{- if .Values.readinessProbe.enabled }}
          readinessProbe:
            {{- toYaml (omit .Values.readinessProbe "enabled") | nindent 12 }}
          {{- end }}
          {{- with .Values.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: data
              mountPath: /data
            {{- with .Values.volumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: data
          {{- if .Values.persistence.enabled }}
          persistentVolumeClaim:
            claimName: {{ include "newsgator.fullname" . }}-data
          {{- else }}
          emptyDir: {}
          {{- end }}
        {{- with .Values.volumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- with .Values.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
