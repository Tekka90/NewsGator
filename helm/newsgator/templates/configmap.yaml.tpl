apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "newsgator.fullname" . }}
  labels:
    {{- include "newsgator.labels" . | nindent 4 }}
data:
  NEWSGATOR_BACKEND_DIR: "/app"
  # LLM (external OpenAI-compatible server)
  LLM_BASE_URL: {{ .Values.config.llm.baseUrl | quote }}
  LLM_MODEL: {{ .Values.config.llm.model | quote }}
  EMBED_BASE_URL: {{ .Values.config.llm.embedBaseUrl | quote }}
  EMBED_MODEL: {{ .Values.config.llm.embedModel | quote }}
  LLM_CONTEXT_TOKENS: {{ .Values.config.llm.contextTokens | quote }}
  LLM_TIMEOUT_S: {{ .Values.config.llm.timeoutS | quote }}
  DISCOVERY_BUDGET_S: {{ .Values.config.llm.discoveryBudgetS | quote }}
  LLM_TRACE_ENABLED: {{ .Values.config.llm.trace.enabled | quote }}
  LLM_TRACE_MAX_CHARS: {{ .Values.config.llm.trace.maxChars | quote }}
  # Language
  SUMMARY_LANGUAGE: {{ .Values.config.language.summary | quote }}
  SUMMARY_MIN_SENTENCES: {{ .Values.config.language.summaryMinSentences | quote }}
  SUMMARY_MAX_SENTENCES: {{ .Values.config.language.summaryMaxSentences | quote }}
  HEADLINE_MIN_WORDS: {{ .Values.config.language.headlineMinWords | quote }}
  HEADLINE_MAX_WORDS: {{ .Values.config.language.headlineMaxWords | quote }}
  # Sharing
  SHARE_LANGUAGES: {{ .Values.config.sharing.languages | quote }}
  # Vector store
  VECTOR_BACKEND: {{ .Values.config.vectorStore.backend | quote }}
  QDRANT_URL: {{ .Values.config.vectorStore.qdrant.url | quote }}
  # Clustering thresholds
  TAU_ATTACH: {{ .Values.config.clustering.tauAttach | quote }}
  TAU_GRAY: {{ .Values.config.clustering.tauGray | quote }}
  FREEZE_AFTER_HOURS: {{ .Values.config.clustering.freezeAfterHours | quote }}
  # Ingestion
  RETENTION_DAYS: {{ .Values.config.ingestion.retentionDays | quote }}
  FEED_DISABLE_AFTER_DAYS: {{ .Values.config.ingestion.feedDisableAfterDays | quote }}
  POLL_INTERVAL_MIN_MINUTES: {{ .Values.config.ingestion.pollIntervalMinMinutes | quote }}
  POLL_INTERVAL_MAX_MINUTES: {{ .Values.config.ingestion.pollIntervalMaxMinutes | quote }}
  FULLTEXT_MIN_CHARS: {{ .Values.config.ingestion.fulltextMinChars | quote }}
  BACKLOG_SWEEP_MINUTES: {{ .Values.config.ingestion.backlogSweepMinutes | quote }}
  ARTICLE_PROCESS_TIMEOUT_MINUTES: {{ .Values.config.ingestion.articleProcessTimeoutMinutes | quote }}
  # Newsletter ingestion
  MAIL_POLL_MINUTES: {{ .Values.config.newsletter.mailPollMinutes | quote }}
  MAIL_MAX_MESSAGES_PER_POLL: {{ .Values.config.newsletter.mailMaxMessagesPerPoll | quote }}
  NEWSLETTER_LLM_CLEAN: {{ .Values.config.newsletter.llmClean | quote }}
  NEWSLETTER_LLM_EXTRACT: {{ .Values.config.newsletter.llmExtract | quote }}
  # Third-party RSS Reader API ingestion
  READER_POLL_MINUTES: {{ .Values.config.reader.pollMinutes | quote }}
  # Readeck integration
  READECK_BASE_URL: {{ .Values.config.readeck.baseUrl | quote }}
  # Chatbot
  CHAT_ENABLED: {{ .Values.config.chat.enabled | quote }}
  CHAT_TOP_K: {{ .Values.config.chat.topK | quote }}
  CHAT_CANDIDATES: {{ .Values.config.chat.candidates | quote }}
  # Category suggestions
  CATEGORY_SUGGESTIONS_ENABLED: {{ .Values.config.categorySuggestions.enabled | quote }}
  CATEGORY_SUGGESTION_MIN_ARTICLES: {{ .Values.config.categorySuggestions.minArticles | quote }}
  CATEGORY_SUGGESTION_WINDOW_DAYS: {{ .Values.config.categorySuggestions.windowDays | quote }}
