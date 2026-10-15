export interface ModelStatus {
  status: 'online' | 'inactive' | 'unavailable' | 'error' | string
  latency_ms?: number
  reason?: string
  active_mode?: string
  message?: string
  incident?: { id: string; at: string; source: string; kind: string; detail: string }
}

export interface GatewayHealth {
  status: string
  service: string
  version: string
  active_mode: string
  online_models: number
  inactive_models: number
  unavailable_models: number
  error_models: number
  total_models: number
  models: Record<string, ModelStatus>
}

export interface HostMetrics {
  architecture?: string
  cpu_percent?: number
  cpu_cores?: number
  load?: number[]
  memory_total?: number
  memory_used?: number
  memory_percent?: number
  disk_total?: number
  disk_used?: number
  disk_free?: number
  disk_percent?: number
}

export interface ContainerRow {
  service: string
  name: string
  state: string
  status: string
  image: string
  cpu_percent?: number
  memory?: string
  memory_percent?: number
  network?: string
}

export interface ModelCtlStatus {
  healthy?: boolean
  configured_mode?: string
  detected_mode?: string
  services?: Record<string, string>
  message?: string
}

export interface StartupVerificationState {
  schema_version: number
  boot_id?: string
  current_boot?: boolean
  source?: 'boot' | 'install' | 'upgrade' | 'manual' | string
  mode?: string
  status: 'waiting' | 'queued' | 'running' | 'succeeded' | 'failed' | 'unknown' | string
  job_id?: string
  stage?: string
  current_model?: string
  completed?: number
  total?: number
  elapsed_seconds?: number
  started_at?: string
  updated_at?: string
  finished_at?: string
  error?: string | null
  evidence_id?: string | null
  verified_models?: string[]
  already_verified?: boolean
}

export interface Overview {
  gateway: GatewayHealth
  host: {
    platform?: {
      id: string
      name: string
      service_kind?: string
      external_management?: boolean
      allow_mode_switch?: boolean
      allow_restart?: boolean
      benchmark_running?: boolean
      mode_label?: string
      notice?: string
      served_models?: string[]
    }
    accelerator_error?: string | null
    accelerators?: Array<{
      device: number
      card: number
      chip: number
      name: string
      health: string
      temperature_c: number
      aicore_percent: number
      memory_used_mb: number
      memory_total_mb: number
      power_w: number | null
    }>
    error?: string
    system: HostMetrics
    containers: ContainerRow[]
    modelctl: ModelCtlStatus
    startup_verification?: StartupVerificationState | null
  }
  refreshed_at: string
}

export interface ModelRecord {
  safety?: ModelStatus | null
  id: string
  object: string
  owned_by?: string
  capability: string
  backend?: string
  enabled?: boolean
  status: string
  modes?: string[]
  active_mode?: string
  metadata?: Record<string, unknown>
}

export interface ModelsResponse { object: string; data: ModelRecord[] }

export interface Job {
  id: string
  kind: string
  target: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'unknown'
  created_at: string
  started_at?: string
  finished_at?: string
  result?: Record<string, unknown>
  error?: string
  progress?: {
    stage: string
    phase?: 'switching' | 'loading' | 'verification' | 'rollback' | 'complete' | 'failed' | string
    current_model?: string
    elapsed_seconds?: number
    completed?: number
    total?: number
    rollback?: boolean
    message?: string
    file?: string
    bytes?: number
    total_bytes?: number
    evidence_id?: string
    steps?: Array<{ at: string; stage: string }>
  }
}

export interface ConfigModel {
  id: string
  capability: string
  enabled: boolean
  base_url: string
  modes: string[]
}

export interface ConfigResponse {
  services: Record<string, string>
  schema: ConfigSchema
  settings: RuntimeSettings
  models: ConfigModel[]
}

export type RuntimeSettingValue = string | number | boolean
export type RuntimeSettings = Record<string, RuntimeSettingValue>

export interface ConfigChoice { value: string; label: string }

export interface ConfigField {
  id: string
  label: string
  type: 'integer' | 'number' | 'boolean' | 'select'
  min?: number
  max?: number
  step?: number
  unit?: string
  help?: string
  risk?: string
  restart?: string
  depends_on?: string
  depends_not?: Record<string, RuntimeSettingValue>
  choices?: ConfigChoice[]
}

export interface ConfigGroup {
  id: string
  label: string
  description: string
  fields: ConfigField[]
}

export interface ConfigProfile {
  id: string
  label: string
  values: RuntimeSettings
}

export interface ConfigSchema {
  version: number
  groups: ConfigGroup[]
  profiles: ConfigProfile[]
}

export interface ConfigValidation {
  valid: boolean
  changes: Array<Record<string, unknown>>
  affected_services: string[]
  estimated_downtime_seconds: number
}
