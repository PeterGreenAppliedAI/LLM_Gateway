// Types
export interface Stats {
  period_hours: number
  total_requests: number
  success_count: number
  error_count: number
  success_rate: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  avg_latency_ms: number
  min_latency_ms: number
  max_latency_ms: number
  requests_by_endpoint: Record<string, number>
  top_models: Record<string, number>
}

export interface Request {
  id: number
  request_id: string
  timestamp: string
  client_id: string
  task: string
  model: string
  endpoint: string
  status: string
  latency_ms: number | null
  prompt_tokens: number
  completion_tokens: number
  error_code: string | null
}

export interface RequestDetail extends Request {
  user_id: string | null
  environment: string | null
  provider_type: string | null
  stream: boolean
  max_tokens: number | null
  temperature: number | null
  error_message: string | null
  time_to_first_token_ms: number | null
  tokens_per_second: number | null
  total_tokens: number
  estimated_cost_usd: number | null
  request_body: string | null
  response_body: string | null
}

export interface Endpoint {
  name: string
  type: string
  url: string
  enabled: boolean
  healthy: boolean
  labels: Record<string, string>
  models: string[]
}

export interface Catalog {
  last_discovery: string
  endpoints: Endpoint[]
  total_models: number
  total_endpoints: number
}

export interface HealthResponse {
  status: string
  providers_configured: number
  providers_healthy: number
  providers: { name: string; status: string; healthy: boolean }[]
}

export interface SecurityAlert {
  timestamp: string
  request_id: string
  client_id: string
  severity: string
  alert_type: string
  description: string
  details: Record<string, unknown>
}

export interface SecurityStats {
  requests_analyzed: number
  alerts_generated: number
  requests_dropped: number
  queue_size: number
  alerts_in_memory: number
  guard_scans: number
  guard_skipped: number
  guard_unsafe: number
}

export interface SecurityResult {
  request_id: string
  analyzed_at: string
  regex_threat_level: string
  regex_match_count: number
  guard_safe: boolean | null
  guard_skipped: boolean | null
  guard_category_code: string | null
  guard_category_name: string | null
  guard_confidence: string | null
  guard_inference_ms: number | null
  guard_error: string | null
  alert_count: number
}

export interface ApiKeyInfo {
  id: number
  prefix: string
  name: string
  client_id: string
  environment: string | null
  created_at: string | null
  last_used_at: string | null
  is_active: boolean
  allowed_endpoints: string[] | null
  allowed_models: string[] | null
  rate_limit_rpm: number | null
  description: string | null
}

export interface BudgetTier {
  name: string
  cost_multiplier: number
  daily_limit: number | null
}

export interface ModelClassification {
  model: string
  tier: string | null
  cost_multiplier: number
  classified: boolean
}

export interface BudgetConfig {
  enabled: boolean
  default_daily_limit: number
  default_cost_multiplier: number
  enforce_pre_request: boolean
  tiers: BudgetTier[]
  model_assignments: Record<string, string>
  model_classifications: ModelClassification[]
}

export interface BudgetKeyUsage {
  key: string
  daily_limit: number
  tokens_used: number
  tokens_remaining: number
  tier_usage: Record<string, number>
  request_count?: number
  resets_at: string
}

export interface BudgetUsage {
  enabled: boolean
  keys: BudgetKeyUsage[]
}

export interface SecurityScan {
  request_id: string
  timestamp: string
  client_id: string
  model: string | null
  task: string | null
  messages: { role: string; content: string }[]
  regex_threat_level: string
  regex_match_count: number
  guard_safe: boolean | null
  guard_skipped: boolean | null
  guard_category_code: string | null
  is_disagreement: boolean
  label: string | null
  label_category: string | null
  labeled_by: string | null
  label_notes: string | null
}

export interface LabelStats {
  total: number
  labeled: number
  unlabeled: number
  safe: number
  unsafe: number
  disagreements: number
}

export interface PIIStats {
  enabled: boolean
  total_detections: number
  by_type: Record<string, number>
  scrubbed_count: number
  flagged_only_count: number
  unique_requests: number
  unique_values: number
}

export interface PIIEvent {
  id: number
  request_id: string
  timestamp: string
  client_id: string
  model: string | null
  task: string | null
  pii_type: string
  message_index: number | null
  message_role: string | null
  position_start: number | null
  position_end: number | null
  value_hash: string
  was_scrubbed: boolean
  scan_time_ms: number | null
}
