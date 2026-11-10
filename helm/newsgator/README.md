# newsgator

![Version: 0.1.0](https://img.shields.io/badge/Version-0.1.0-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square) ![AppVersion: 1.7.0](https://img.shields.io/badge/AppVersion-1.7.0-informational?style=flat-square)

A Helm chart for NewsGator subcomponent newsgator

## Maintainers

| Name | Email | Url |
| ---- | ------ | --- |
| webofmars | <contact@webofmars.com> |  |
| Tekka90 | <dev@Tekka90.com> |  |

## Values

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| affinity | object | `{}` |  |
| autoscaling.enabled | bool | `false` |  |
| autoscaling.maxReplicas | int | `100` |  |
| autoscaling.minReplicas | int | `1` |  |
| autoscaling.targetCPUUtilizationPercentage | int | `80` |  |
| common | object | `{}` | bitnami common dependency values |
| config.categorySuggestions.enabled | bool | `true` | Enable category-suggestion detection |
| config.categorySuggestions.minArticles | int | `5` | Distinct articles before proposing |
| config.categorySuggestions.windowDays | int | `30` | Rolling window for the count |
| config.chat.candidates | int | `40` | ANN candidates re-ranked per question |
| config.chat.enabled | bool | `true` | Enable the /chat endpoint |
| config.chat.topK | int | `12` | Stories handed to the LLM as grounding |
| config.clustering.freezeAfterHours | int | `72` | Hours after which a story is frozen and no longer accepts new sources |
| config.clustering.tauAttach | float | `0.82` | Cosine-similarity threshold to attach an article to an existing story |
| config.clustering.tauGray | float | `0.7` | Lower bound of the gray zone triggering a pairwise LLM decision |
| config.databaseUrl | string | `"sqlite+aiosqlite:////data/newsgator.db"` | SQLAlchemy async database URL. Stored in the Secret because it may embed credentials. The default points at the persistent volume mounted at /data. |
| config.ingestion.articleProcessTimeoutMinutes | int | `15` | Timeout in minutes for processing a single article through all stages |
| config.ingestion.backlogSweepMinutes | int | `30` | How often articles stuck mid-pipeline (LLM errors, restarts) are retried |
| config.ingestion.feedDisableAfterDays | int | `7` | Disable a feed after this many days of consecutive failures |
| config.ingestion.fulltextMinChars | int | `400` | Below this extracted-text length the archive.is fallback is tried |
| config.ingestion.pollIntervalMaxMinutes | int | `60` | Maximum feed poll interval in minutes |
| config.ingestion.pollIntervalMinMinutes | int | `15` | Minimum feed poll interval in minutes |
| config.ingestion.retentionDays | int | `45` | Days articles/stories are retained before the nightly purge |
| config.language.headlineMaxWords | int | `12` | Headline length guidance (1-40, must be >= headlineMinWords) |
| config.language.headlineMinWords | int | `0` | Headline length guidance (0 = no minimum in the prompt, max 40) |
| config.language.summary | string | `"en"` | Language all summaries/embeddings are produced in (ISO 639-1) |
| config.language.summaryMaxSentences | int | `5` | Story summary length guidance, merge + headline prompts only (1-12) |
| config.language.summaryMinSentences | int | `3` | Story summary length guidance, merge + headline prompts only (1-12, must be <= summaryMaxSentences) |
| config.llm.apiKey | string | `""` | Bearer token sent to the LLM server. OpenRouter requires a real API key; local servers usually ignore it. Stored in the Secret. |
| config.llm.baseUrl | string | `"https://openrouter.ai/api/v1"` | Base URL of the external OpenAI-compatible LLM server |
| config.llm.contextTokens | int | `32768` | Model context window in tokens; longer articles are condensed map-reduce style |
| config.llm.discoveryBudgetS | int | `40` | Feed discovery time budget in seconds; partial results are returned after it |
| config.llm.embedBaseUrl | string | `""` | Embeddings endpoint. Empty = defaults to llm.baseUrl. |
| config.llm.embedModel | string | `"baai/bge-m3"` | Embeddings model (must output 1024 dimensions) |
| config.llm.model | string | `"meta-llama/llama-3.3-70b-instruct"` | Chat model name served by the LLM server |
| config.llm.timeoutS | int | `120` | LLM request timeout in seconds (long articles on slow models need headroom) |
| config.llm.trace.enabled | bool | `true` | Enable the live LLM interaction trace |
| config.llm.trace.maxChars | int | `8000` | Per-message clamp; longer text is truncated |
| config.newsletter.llmClean | bool | `true` | One LLM call first deletes the non-news chrome (intro, socials, sponsors, platform self-links) from the full email before link extraction |
| config.newsletter.llmExtract | bool | `true` | One LLM call per newsletter maps links to their intro text; false = code heuristic only |
| config.newsletter.mailMaxMessagesPerPoll | int | `20` | Cap per account per poll (oldest first) |
| config.newsletter.mailPollMinutes | int | `15` | How often all IMAP accounts are polled |
| config.readeck.baseUrl | string | `""` | Readeck instance base URL. Empty = integration disabled. |
| config.readeck.token | string | `""` | Readeck API token. Stored in the Secret, only rendered when set. |
| config.reader.pollMinutes | int | `15` | How often all connected reader accounts are polled |
| config.secretKey | string | "" (auto-generated) | Session-cookie signing secret. When left empty, a random 64-character key is generated on first install and reused on upgrades. Stored in the Secret. |
| config.sharing.languages | string | `"en,fr,de,es,it,pt,nl"` | Languages offered by the share button (comma-separated ISO 639-1 codes). Picking one translates the story headline + summary on demand via the LLM; "as is" sharing never calls the LLM. |
| config.vectorStore.backend | string | `"sqlite_vec"` | Vector backend: sqlite_vec (default, zero-ops) or qdrant (external server - never spun up by this chart) @schema enum: [sqlite_vec, qdrant] @schema |
| config.vectorStore.qdrant.apiKey | string | `""` | Qdrant API key. Stored in the Secret, only rendered when set. |
| config.vectorStore.qdrant.url | string | `""` | Qdrant base URL. Empty = unset. |
| fullnameOverride | string | `""` |  |
| httpRoute | object | `{"annotations":{},"enabled":false,"hostnames":["chart-example.local"],"parentRefs":[{"name":"gateway","sectionName":"http"}],"rules":[{"matches":[{"path":{"type":"PathPrefix","value":"/headers"}}]}]}` | Expose the service via gateway-api HTTPRoute Requires Gateway API resources and suitable controller installed within the cluster (see: https://gateway-api.sigs.k8s.io/guides/) |
| image.pullPolicy | string | `"IfNotPresent"` |  |
| image.repository | string | `"tekka90/newsgator"` |  |
| image.tag | string | `""` |  |
| imagePullSecrets | list | `[]` |  |
| ingress.annotations | object | `{}` |  |
| ingress.className | string | `""` |  |
| ingress.enabled | bool | `false` |  |
| ingress.hosts[0].host | string | `"chart-example.local"` |  |
| ingress.hosts[0].paths[0].path | string | `"/"` |  |
| ingress.hosts[0].paths[0].pathType | string | `"ImplementationSpecific"` |  |
| ingress.tls | list | `[]` |  |
| livenessProbe.enabled | bool | `true` |  |
| livenessProbe.httpGet.path | string | `"/api/health"` |  |
| livenessProbe.httpGet.port | string | `"http-frontend"` |  |
| nameOverride | string | `""` |  |
| nodeSelector | object | `{}` |  |
| persistence | object | `{"accessModes":["ReadWriteOnce"],"enabled":false,"size":"1Gi"}` | enable persistence using a PersistentVolumeClaim |
| podAnnotations | object | `{}` |  |
| podLabels | object | `{}` |  |
| podSecurityContext | object | `{}` |  |
| readinessProbe.enabled | bool | `true` |  |
| readinessProbe.httpGet.path | string | `"/api/health"` |  |
| readinessProbe.httpGet.port | string | `"http-backend"` |  |
| replicaCount | int | `1` |  |
| resources | object | `{}` | resources configuration for the container |
| securityContext | object | `{}` |  |
| service | object | `{"port":8000,"type":"ClusterIP"}` | backend port |
| serviceAccount.annotations | object | `{}` |  |
| serviceAccount.automount | bool | `true` |  |
| serviceAccount.create | bool | `true` |  |
| serviceAccount.name | string | `""` |  |
| serviceFrontend | object | `{"port":3000,"type":"ClusterIP"}` | frontend port |
| startupProbe.enabled | bool | `false` |  |
| startupProbe.failureThreshold | int | `10` |  |
| startupProbe.httpGet.path | string | `"/api/health"` |  |
| startupProbe.httpGet.port | string | `"http-backend"` |  |
| startupProbe.initialDelaySeconds | int | `30` |  |
| tolerations | list | `[]` |  |
| volumeMounts | list | `[]` |  |
| volumes | list | `[]` |  |

----------------------------------------------
Autogenerated from chart metadata using [helm-docs v1.14.2](https://github.com/norwoodj/helm-docs/releases/v1.14.2)
