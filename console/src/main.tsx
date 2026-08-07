import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCopy,
  Download,
  FileArchive,
  Loader2,
  Network,
  PackageOpen,
  RefreshCcw,
  Upload,
  Wrench,
} from 'lucide-react'
import './style.css'

type Artifact = {
  artifact_id: string
  phase_key: string
  import_id?: string | null
  name: string
  kind: string
  source: string
  size_bytes?: number | null
  modified_at?: string | null
  download_url: string
}

type PhaseArtifact = {
  phase_key: string
  label: string
  status: string
  import_id?: string | null
  core_nodes: number
  core_relationships: number
  gap_entries: number
  skipped_relationships: number
  artifact_count: number
  artifacts: Artifact[]
  summary?: any
  overlay_data_result?: any
}

type PhaseArtifactIndex = {
  generated_at: string
  purpose: string
  phases: PhaseArtifact[]
  totals: Record<string, number>
  links?: Record<string, string>
  notes?: string[]
}



type AddonInputSummary = {
  addon_input_id: string
  source_import_id?: string | null
  addon_name?: string | null
  display_name?: string | null
  status?: string | null
  validation_status?: string | null
  total_records?: number
  record_counts?: Record<string, number>
  status_counts?: Record<string, number>
  imported_at?: string | null
  apply_status?: string | null
  addon_path?: string | null
}

type LogLine = { level: 'info' | 'ok' | 'warn' | 'error'; text: string }

const ENV_API_BASE = import.meta.env.VITE_API_BASE_URL as string | undefined
const apiCandidates = (() => {
  const list = ['/api']
  if (ENV_API_BASE && !list.includes(ENV_API_BASE)) list.push(ENV_API_BASE)
  if (!list.includes('http://localhost:18181')) list.push('http://localhost:18181')
  return list
})()

const phaseOrder = ['P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7']
const phaseLabels: Record<string, string> = {
  P1: 'Standard Replacement',
  P2: 'Standard Configuration',
  P3: 'Minor Custom',
  P4: 'Custom Model',
  P5: 'Custom Logic',
  P6: 'Diagrams',
  P7: 'Authority / Organization',
}

function fmt(n?: number | null) { return Number(n || 0).toLocaleString() }
function bytes(n?: number | null) {
  const v = Number(n || 0)
  if (!v) return '-'
  if (v > 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`
  if (v > 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${v} B`
}
function statusLabel(status?: string) {
  const map: Record<string, string> = {
    not_imported: '未Import',
    imported: 'Import済',
    json_imported: 'Import済',
    validation_failed: '要修正',
    ready_for_core_apply: 'Core準備OK',
    ready_for_neo4j_apply: 'Neo4j準備OK',
    p3_neo4j_applied: 'P3 Neo4j反映済',
    p3_addon_input_generated: 'Addon Input生成済',
    p3_addon_input_validated: 'Addon Input検証OK',
    p3_addon_input_validation_failed: 'Addon Input検証NG',
    next_ready: '次工程準備OK',
    neo4j_applied: 'Neo4j反映済',
    neo4j_core_applied: 'Core反映済',
    odoo_addon_generated: 'Addon生成済',
    odoo_overlay_data_generated: 'Overlay生成済',
    dry_run_ok: 'Dry Run OK',
    failed: '失敗',
  }
  return map[status || ''] || status || '未済'
}
function statusTone(status?: string) {
  if (['imported', 'json_imported', 'ready_for_core_apply', 'ready_for_neo4j_apply', 'p3_neo4j_applied', 'p3_addon_input_generated', 'p3_addon_input_validated', 'next_ready', 'neo4j_applied', 'neo4j_core_applied', 'odoo_addon_generated', 'odoo_overlay_data_generated', 'dry_run_ok'].includes(status || '')) return 'good'
  if (['validation_failed', 'p3_addon_input_validation_failed', 'failed'].includes(status || '')) return 'bad'
  if (['context_repair_required'].includes(status || '')) return 'warn'
  if (['not_imported', undefined as any].includes(status as any)) return 'muted'
  return 'neutral'
}
function artifactKindLabel(kind: string) {
  const map: Record<string, string> = {
    zip: 'ZIP', markdown: 'MD', summary_json: 'Summary', fg_gap_json: 'GAP JSON', payload_json: 'Payload', result_json: 'Result', json: 'JSON', text: 'Text', file: 'File'
  }
  return map[kind] || kind
}
function downloadHref(apiBase: string, url: string) {
  if (url.startsWith('http')) return url
  return `${apiBase || '/api'}${url}`
}
async function tryFetch(path: string, init?: RequestInit): Promise<{ res: Response; base: string }> {
  const errors: string[] = []
  for (const base of apiCandidates) {
    try {
      const controller = new AbortController()
      const timer = window.setTimeout(() => controller.abort(), 180000)
      const res = await fetch(`${base}${path}`, { ...init, signal: controller.signal })
      window.clearTimeout(timer)
      return { res, base }
    } catch (e: any) {
      errors.push(`${base}: ${e?.message || String(e)}`)
    }
  }
  throw new Error(`API fetch failed. Tried: ${errors.join(' / ')}`)
}
async function readJsonSafe(res: Response) {
  const text = await res.text()
  try { return JSON.parse(text) } catch { return { raw_text: text } }
}
function errorMessage(data: any, fallback: string) {
  const detail = data?.detail ?? data?.error ?? data?.message ?? data?.raw_text
  if (detail == null) return fallback
  if (typeof detail === 'string') return detail
  try { return JSON.stringify(detail, null, 2) } catch { return String(detail) }
}
function Pill({ status }: { status: string }) { return <b className={`pill ${statusTone(status)}`}>{statusLabel(status)}</b> }
function Metric({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return <div className={`metric ${tone || ''}`}><span>{label}</span><strong>{fmt(value)}</strong></div>
}

class ConsoleErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  constructor(props: { children: React.ReactNode }) { super(props); this.state = { error: null } }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (this.state.error) {
      return <main className="app-shell"><section className="panel"><div className="section-title"><AlertTriangle size={18}/> Console rendering error</div><pre className="error-box">{this.state.error.message}</pre></section></main>
    }
    return this.props.children
  }
}

function App() {
  const [index, setIndex] = useState<PhaseArtifactIndex | null>(null)
  const [selectedPhaseKey, setSelectedPhaseKey] = useState('P2')
  const [p1p2File, setP1P2File] = useState<File | null>(null)
  const [p3File, setP3File] = useState<File | null>(null)
  const [p3ImportResult, setP3ImportResult] = useState<any | null>(null)
  const [p3InspectionResult, setP3InspectionResult] = useState<any | null>(null)
  const [p3AddonInputResult, setP3AddonInputResult] = useState<any | null>(null)
  const [p3AddonInputValidationResult, setP3AddonInputValidationResult] = useState<any | null>(null)
  const [p3CodegenMaterialResult, setP3CodegenMaterialResult] = useState<any | null>(null)
  const [p3GeneratedCodeFile, setP3GeneratedCodeFile] = useState<File | null>(null)
  const [p3GeneratedCodeImportResult, setP3GeneratedCodeImportResult] = useState<any | null>(null)
  const [p3OdooApplyResult, setP3OdooApplyResult] = useState<any | null>(null)
  const [addonInputFile, setAddonInputFile] = useState<File | null>(null)
  const [addonInputs, setAddonInputs] = useState<AddonInputSummary[]>([])
  const [selectedAddonInputId, setSelectedAddonInputId] = useState('')
  const [promptPackResult, setPromptPackResult] = useState<any | null>(null)
  const [addonInputResult, setAddonInputResult] = useState<any | null>(null)
  const [addonApplyResult, setAddonApplyResult] = useState<any | null>(null)
  const [busy, setBusy] = useState(false)
  const [apiBaseUsed, setApiBaseUsed] = useState('')
  const [logs, setLogs] = useState<LogLine[]>([{ level: 'info', text: 'P1〜P7 Phase Matrixを読み込みます。P2はP1/P2 GAP-aware成果物を表示します。' }])
  const [p5InternalDesignFile, setP5InternalDesignFile] = useState<File | null>(null)
  const [p5InternalDesignImports, setP5InternalDesignImports] = useState<any[]>([])
  const [p5SelectedInternalDesignId, setP5SelectedInternalDesignId] = useState('')
  const [p5InternalDesignImportResult, setP5InternalDesignImportResult] = useState<any | null>(null)
  const [p5InternalDesignValidation, setP5InternalDesignValidation] = useState<any | null>(null)
  const [p5InternalDesignPreview, setP5InternalDesignPreview] = useState<any | null>(null)
  const [p5Neo4jDryRunResult, setP5Neo4jDryRunResult] = useState<any | null>(null)
  const [p5Neo4jApplyResult, setP5Neo4jApplyResult] = useState<any | null>(null)
  const [p6DiagramFile, setP6DiagramFile] = useState<File | null>(null)
  const [p6DiagramPacks, setP6DiagramPacks] = useState<any[]>([])
  const [p6SelectedPackId, setP6SelectedPackId] = useState('')
  const [p6ImportResult, setP6ImportResult] = useState<any | null>(null)
  const [p6ValidationResult, setP6ValidationResult] = useState<any | null>(null)
  const [p7AuthorityFile, setP7AuthorityFile] = useState<File | null>(null)
  const [p7AuthorityPacks, setP7AuthorityPacks] = useState<any[]>([])
  const [p7SelectedAuthorityId, setP7SelectedAuthorityId] = useState('')
  const [p7SelectedViewKey, setP7SelectedViewKey] = useState('view.organization_overview')
  const [p7ViewPayload, setP7ViewPayload] = useState<any | null>(null)
  const [p7SelectedElement, setP7SelectedElement] = useState<any | null>(null)
  const [p7ImportResult, setP7ImportResult] = useState<any | null>(null)
  const [p7ValidationResult, setP7ValidationResult] = useState<any | null>(null)
  const [p3InternalBindingFile, setP3InternalBindingFile] = useState<File | null>(null)
  const [p3InternalBindings, setP3InternalBindings] = useState<any[]>([])
  const [selectedP3InternalBindingId, setSelectedP3InternalBindingId] = useState('')
  const [p3InternalBindingImportResult, setP3InternalBindingImportResult] = useState<any | null>(null)
  const [p3InternalBindingValidationResult, setP3InternalBindingValidationResult] = useState<any | null>(null)

  const phases = useMemo(() => {
    const existing = new Map((index?.phases || []).map((p) => [p.phase_key, p]))
    return phaseOrder.map((key) => existing.get(key) || ({ phase_key: key, label: phaseLabels[key], status: 'not_imported', import_id: null, core_nodes: 0, core_relationships: 0, gap_entries: 0, skipped_relationships: 0, artifact_count: 0, artifacts: [] } as PhaseArtifact))
  }, [index])
  const selected = phases.find((p) => p.phase_key === selectedPhaseKey) || phases[0]
  const selectedArtifacts = selected?.artifacts || []
  const selectedAddonInput = addonInputs.find((x) => x.addon_input_id === selectedAddonInputId) || addonInputs[0] || null
  const selectedP5InternalDesign = p5InternalDesignImports.find((x) => x.design_import_id === p5SelectedInternalDesignId) || p5InternalDesignImports[0] || null
  const selectedP6DiagramPack = p6DiagramPacks.find((x) => x.diagram_import_id === p6SelectedPackId) || p6DiagramPacks[0] || null
  const selectedP7AuthorityPack = p7AuthorityPacks.find((x) => x.authority_import_id === p7SelectedAuthorityId) || p7AuthorityPacks[0] || null
  const selectedP3InternalBinding = p3InternalBindings.find((x) => x.binding_import_id === selectedP3InternalBindingId) || p3InternalBindings[0] || null

  function addLog(level: LogLine['level'], text: string) { setLogs((prev) => [{ level, text }, ...prev].slice(0, 80)) }
  async function loadPhaseArtifacts(silent = false) {
    try {
      const { res, base } = await tryFetch('/phase-artifacts')
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Phase artifacts load failed'))
      setIndex(data)
      if (!silent) addLog('ok', `Phase Artifact Indexを読み込みました。API=${base}`)
      const current = data.phases?.find((p: PhaseArtifact) => p.phase_key === selectedPhaseKey)
      if (!current && data.phases?.[0]) setSelectedPhaseKey(data.phases[0].phase_key)
      await loadAddonInputs(true)
      await loadP3InternalBindings(true)
      if (selectedPhaseKey === 'P2') await loadPromptPackResult(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
  }
  useEffect(() => { loadPhaseArtifacts(true) }, [])

  async function loadAddonInputs(silent = false) {
    try {
      const { res, base } = await tryFetch('/p1p2/addon-inputs')
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Addon inputs load failed'))
      const items = data.items || data.addon_inputs || []
      setAddonInputs(items)
      if (!selectedAddonInputId && items[0]?.addon_input_id) setSelectedAddonInputId(items[0].addon_input_id)
      if (!silent) addLog('ok', `Addon Input一覧を読み込みました。${items.length || 0}件`)
    } catch (e: any) {
      if (!silent) addLog('warn', `Addon Input一覧はまだ取得できません: ${e?.message || String(e)}`)
    }
  }
  async function loadP6DiagramPacks(silent = false) {
    try {
      const { res, base } = await tryFetch('/p6/diagram-packs')
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P6 diagram packs load failed'))
      const items = data.items || []
      setP6DiagramPacks(items)
      if (!p6SelectedPackId && items[0]?.diagram_import_id) setP6SelectedPackId(items[0].diagram_import_id)
      if (!silent) addLog('ok', `P6 Diagram Pack一覧を読み込みました。${items.length || 0}件`)
    } catch (e: any) {
      if (!silent) addLog('warn', `P6 Diagram Pack一覧はまだ取得できません: ${e?.message || String(e)}`)
    }
  }

  async function uploadP6DiagramPack() {
    if (!p6DiagramFile) return
    setBusy(true); setP6ImportResult(null); setP6ValidationResult(null)
    addLog('info', `Uploading P6 P3 Diagram Pack ${p6DiagramFile.name}...`)
    const form = new FormData(); form.append('file', p6DiagramFile)
    try {
      const { res, base } = await tryFetch('/p6/diagram-packs/import', { method: 'POST', body: form })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P6 Diagram Pack import failed'))
      setP6ImportResult(data)
      setP6SelectedPackId(data.diagram_import_id || '')
      setSelectedPhaseKey('P6')
      addLog(data.status === 'validation_failed' ? 'warn' : 'ok', `P6 Diagram Pack Import完了: actions=${data.summary?.action_count || 0} / graphs=${data.summary?.graph_file_count || 0}`)
      await loadP6DiagramPacks(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP6ImportResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function validateP6DiagramPack() {
    const id = selectedP6DiagramPack?.diagram_import_id
    if (!id) return
    setBusy(true); setP6ValidationResult(null)
    try {
      const { res, base } = await tryFetch(`/p6/diagram-packs/${id}/validate`, { method: 'POST' })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P6 Diagram Pack validate failed'))
      setP6ValidationResult(data)
      addLog(data.valid ? 'ok' : 'warn', `P6 Validate: ${data.status || '-'} / errors=${data.error_count || 0} / warnings=${data.warning_count || 0}`)
      await loadP6DiagramPacks(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP6ValidationResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }


  async function loadP7AuthorityPacks(silent = false) {
    try {
      const { res, base } = await tryFetch('/p7/authority-packs')
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P7 authority packs load failed'))
      const items = data.items || []
      setP7AuthorityPacks(items)
      if (!p7SelectedAuthorityId && items[0]?.authority_import_id) setP7SelectedAuthorityId(items[0].authority_import_id)
      if (!p7SelectedViewKey && items[0]?.views?.[0]?.view_key) setP7SelectedViewKey(items[0].views[0].view_key)
      if (!silent) addLog('ok', `P7 Authority Pack一覧を読み込みました。${items.length || 0}件`)
    } catch (e: any) {
      if (!silent) addLog('warn', `P7 Authority Pack一覧はまだ取得できません: ${e?.message || String(e)}`)
    }
  }

  async function uploadP7AuthorityPack() {
    if (!p7AuthorityFile) return
    setBusy(true); setP7ImportResult(null); setP7ValidationResult(null); setP7ViewPayload(null); setP7SelectedElement(null)
    addLog('info', `Uploading P7 Authority Pack ${p7AuthorityFile.name}...`)
    const form = new FormData(); form.append('file', p7AuthorityFile)
    try {
      const { res, base } = await tryFetch('/p7/authority-packs/import', { method: 'POST', body: form })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P7 Authority Pack import failed'))
      setP7ImportResult(data)
      setP7SelectedAuthorityId(data.authority_import_id || '')
      const firstView = data.views?.[0]?.view_key || 'view.organization_overview'
      setP7SelectedViewKey(firstView)
      setSelectedPhaseKey('P7')
      addLog(data.status === 'validation_failed' ? 'warn' : 'ok', `P7 Authority Import完了: views=${data.summary?.view_count || 0} / nodes=${data.summary?.node_count || 0} / edges=${data.summary?.edge_count || 0}`)
      await loadP7AuthorityPacks(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP7ImportResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function validateP7AuthorityPack() {
    const id = selectedP7AuthorityPack?.authority_import_id
    if (!id) return
    setBusy(true); setP7ValidationResult(null)
    try {
      const { res, base } = await tryFetch(`/p7/authority-packs/${id}/validate`, { method: 'POST' })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P7 Authority Pack validate failed'))
      setP7ValidationResult(data)
      addLog(data.valid ? 'ok' : 'warn', `P7 Validate: ${data.status || '-'} / errors=${data.error_count || 0} / warnings=${data.warning_count || 0}`)
      await loadP7AuthorityPacks(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP7ValidationResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function loadP7AuthorityView(viewKey = p7SelectedViewKey) {
    const id = selectedP7AuthorityPack?.authority_import_id || p7SelectedAuthorityId
    if (!id || !viewKey) return
    setBusy(true); setP7ViewPayload(null); setP7SelectedElement(null)
    try {
      const { res, base } = await tryFetch(`/p7/authority-packs/${id}/views/${encodeURIComponent(viewKey)}`)
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P7 Authority View load failed'))
      setP7ViewPayload(data.payload || data)
      addLog('ok', `P7 View読込: ${viewKey}`)
    } catch (e: any) {
      addLog('error', e?.message || String(e))
    } finally { setBusy(false) }
  }

  async function loadP3InternalBindings(silent = false) {
    try {
      const { res, base } = await tryFetch('/p4/internal-p3-bindings')
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P3 Internal Binding list load failed'))
      const items = data.items || []
      setP3InternalBindings(items)
      if (!selectedP3InternalBindingId && items[0]?.binding_import_id) setSelectedP3InternalBindingId(items[0].binding_import_id)
      if (!silent) addLog('ok', `P3 Internal Binding一覧を読み込みました。${items.length || 0}件`)
    } catch (e: any) {
      if (!silent) addLog('warn', `P3 Internal Binding一覧はまだ取得できません: ${e?.message || String(e)}`)
    }
  }

  async function uploadP3InternalBinding() {
    if (!p3InternalBindingFile) return
    setBusy(true); setP3InternalBindingImportResult(null); setP3InternalBindingValidationResult(null)
    addLog('info', `Uploading P3 Internal Binding Pack ${p3InternalBindingFile.name}...`)
    const form = new FormData(); form.append('file', p3InternalBindingFile)
    try {
      const { res, base } = await tryFetch('/p4/internal-p3-bindings/import', { method: 'POST', body: form })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P3 Internal Binding import failed'))
      setP3InternalBindingImportResult(data)
      setSelectedP3InternalBindingId(data.binding_import_id || '')
      setSelectedPhaseKey('P6')
      addLog(data.status === 'validation_failed' ? 'warn' : 'ok', `P3 Internal Binding Import完了: nodes=${data.counts?.nodes || 0} / edges=${data.counts?.edges || 0}`)
      await loadP3InternalBindings(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP3InternalBindingImportResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function validateP3InternalBinding() {
    const id = selectedP3InternalBinding?.binding_import_id
    if (!id) return
    setBusy(true); setP3InternalBindingValidationResult(null)
    try {
      const { res, base } = await tryFetch(`/p4/internal-p3-bindings/${id}/validate`, { method: 'POST' })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P3 Internal Binding validate failed'))
      setP3InternalBindingValidationResult(data)
      addLog(data.valid ? 'ok' : 'warn', `P3 Internal Binding Validate: ${data.status || '-'} / errors=${data.error_count || 0} / warnings=${data.warning_count || 0}`)
      await loadP3InternalBindings(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP3InternalBindingValidationResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function loadPromptPackResult(silent = false) {
    if (!selected?.import_id || selected.phase_key !== 'P2') return
    try {
      const { res, base } = await tryFetch(`/p1p2/imports/${selected.import_id}/addon-mapping-prompt-pack`)
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Prompt pack result load failed'))
      setPromptPackResult(data)
      if (!silent) addLog('ok', `ChatGPT Prompt Pack情報を読み込みました。`)
    } catch (e: any) {
      setPromptPackResult(null)
      if (!silent) addLog('warn', `Prompt Packは未生成です: ${e?.message || String(e)}`)
    }
  }
  async function exportPromptPack() {
    if (!selected?.import_id || selected.phase_key !== 'P2') return
    setBusy(true); addLog('info', 'ChatGPT Prompt Packを生成します...')
    try {
      const { res, base } = await tryFetch(`/p1p2/imports/${selected.import_id}/export-addon-mapping-prompt-pack`, { method: 'POST' })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Prompt Pack export failed'))
      setPromptPackResult(data)
      addLog('ok', `Prompt Pack生成完了: ${data.status || 'ok'}`)
      await loadPhaseArtifacts(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
    finally { setBusy(false) }
  }
  async function uploadAddonInputCandidate() {
    if (!addonInputFile) return
    setAddonInputResult(null)
    setAddonApplyResult(null)
    setBusy(true); addLog('info', `Uploading Addon Input Candidate ${addonInputFile.name}...`)
    try {
      const fd = new FormData(); fd.append('file', addonInputFile)
      const { res, base } = await tryFetch('/p1p2/addon-inputs/import', { method: 'POST', body: fd })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Addon Input import failed'))
      const addonInputId = data.addon_input_id || data.summary?.addon_input_id
      setAddonInputResult(data)
      if (addonInputId) setSelectedAddonInputId(addonInputId)
      addLog(data.validation_status === 'ok' || data.status === 'addon_input_imported' ? 'ok' : 'warn', `Addon Input Import完了: ${data.validation_status || data.status || 'ok'} / records=${data.total_records || data.summary?.total_records || '-'}`)
      await loadAddonInputs(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setAddonInputResult({ status: 'error', message })
      addLog('error', message)
    }
    finally { setBusy(false) }
  }
  async function applyOdooAddonDirect() {
    const addonInputId = selectedAddonInput?.addon_input_id
    if (!addonInputId) return
    setAddonApplyResult(null)
    setBusy(true); addLog('info', `custom_addonsへOdoo Addonを直接反映します: ${addonInputId}`)
    try {
      const { res } = await tryFetch(`/p1p2/addon-inputs/${addonInputId}/apply-odoo-addon`, { method: 'POST' })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Odoo Addon direct apply failed'))
      setAddonApplyResult(data)
      addLog('ok', `Odoo Addon反映完了: ${data.status || 'ok'} / ${data.addon_path || ''}`)
      await loadAddonInputs(true)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setAddonApplyResult({ status: 'error', message })
      addLog('error', message)
    }
    finally { setBusy(false) }
  }

  async function uploadP1P2Pack() {
    if (!p1p2File) return
    setBusy(true); addLog('info', `Uploading P1/P2 Combined ${p1p2File.name}...`)
    try {
      const fd = new FormData(); fd.append('file', p1p2File)
      const { res, base } = await tryFetch('/p1p2/import-gap-aware-pack', { method: 'POST', body: fd })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P1/P2 import failed'))
      setSelectedPhaseKey('P2')
      addLog(data.ready_for_core_apply ? 'ok' : 'warn', `P1/P2 Import完了: Core=${data.p1p2_summary?.core_nodes || 0} nodes / GAP=${data.p1p2_summary?.gap_entries || 0}`)
      await loadPhaseArtifacts(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
    finally { setBusy(false) }
  }
  async function uploadP3Neo4jFirstPack() {
    if (!p3File) return
    setP3ImportResult(null)
    setBusy(true); addLog('info', `Uploading P3 Neo4j-first pack ${p3File.name}...`)
    try {
      const fd = new FormData(); fd.append('file', p3File)
      const { res, base } = await tryFetch('/p3/import-neo4j-first-pack', { method: 'POST', body: fd })
      setApiBaseUsed(base)
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P3 Neo4j-first import failed'))
      setP3ImportResult(data)
      setSelectedPhaseKey('P3')
      const counts = data.count_summary || {}
      const pre = data.p3_preclassification_summary || {}
      addLog(data.ready_for_neo4j_import ? 'ok' : 'warn', `P3 Import完了: ${counts.nodes || 0} nodes / ${counts.relationships || 0} rels / unresolved=${pre.unresolved_reference_count || 0}`)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP3ImportResult({ status: 'error', message })
      addLog('error', message)
    }
    finally { setBusy(false) }
  }

  async function runP3Action(action: 'dry-run' | 'apply' | 'inspect' | 'addon' | 'validate' | 'codegen') {
    if (!selected?.import_id || selected.phase_key !== 'P3') return
    const importId = selected.import_id
    const paths: Record<typeof action, { path: string; label: string }> = {
      'dry-run': { path: `/p3/imports/${importId}/neo4j-dry-run`, label: 'P3 Neo4j Dry Run' },
      apply: { path: `/p3/imports/${importId}/apply-neo4j`, label: 'P3 Apply Neo4j' },
      inspect: { path: `/p3/imports/${importId}/burnin-inspection`, label: 'B. P3 Burn-in Candidate Inspection' },
      addon: { path: `/p3/imports/${importId}/addon-input`, label: 'C. P3 Addon Input生成' },
      validate: { path: `/p3/imports/${importId}/addon-input/validate`, label: 'D. P3 Addon Input Validate' },
      codegen: { path: `/p3/imports/${importId}/codegen-material-pack`, label: 'E. Odoo Codegen Material Pack Export' },
    }
    setBusy(true); addLog('info', `${paths[action].label}を実行します...`)
    try {
      const { res } = await tryFetch(paths[action].path, { method: 'POST' })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, `${paths[action].label} failed`))
      addLog('ok', `${paths[action].label}完了: ${data.status || data.summary?.status || 'ok'}`)
      if (action === 'inspect') setP3InspectionResult(data)
      if (action === 'addon') setP3AddonInputResult(data)
      if (action === 'validate') setP3AddonInputValidationResult(data)
      if (action === 'codegen') setP3CodegenMaterialResult(data)
      await loadPhaseArtifacts(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
    finally { setBusy(false) }
  }


  async function uploadP3GeneratedOdooCodePack() {
    if (!selected?.import_id || selected.phase_key !== 'P3' || !p3GeneratedCodeFile) return
    const importId = selected.import_id
    const fd = new FormData()
    fd.append('file', p3GeneratedCodeFile)
    setP3GeneratedCodeImportResult(null)
    setBusy(true); addLog('info', `Uploading Odoo addon code ZIP ${p3GeneratedCodeFile.name}...`)
    try {
      const { res } = await tryFetch(`/p3/imports/${importId}/generated-odoo-code-pack`, { method: 'POST', body: fd })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Odoo code ZIP import failed'))
      setP3GeneratedCodeImportResult(data)
      addLog(data.valid ? 'ok' : 'warn', `F Odoo Code Import: ${data.status || 'ok'} / kind=${data.pack_kind || '-'} / addons=${data.summary?.addon_count ?? '-'}`)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP3GeneratedCodeImportResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }


  async function applyP3OdooAddonDirect() {
    if (!selected?.import_id || selected.phase_key !== 'P3') return
    const importId = selected.import_id
    setP3OdooApplyResult(null)
    setBusy(true); addLog('info', 'G. Apply Odoo Addon Directを実行します...')
    try {
      const { res } = await tryFetch(`/p3/imports/${importId}/apply-odoo-addon-direct`, { method: 'POST' })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'Apply Odoo Addon Direct failed'))
      setP3OdooApplyResult(data)
      addLog('ok', `G Apply完了: ${data.status || 'ok'} / addons=${data.applied_addon_count ?? '-'}`)
      await loadPhaseArtifacts(true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP3OdooApplyResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function runP2Action(action: 'repair' | 'dry-run' | 'apply' | 'overlay') {
    if (!selected?.import_id || selected.phase_key !== 'P2') return
    const importId = selected.import_id
    const paths: Record<typeof action, { path: string; label: string }> = {
      repair: { path: `/p1p2/imports/${importId}/repair-context`, label: 'Context Repair' },
      'dry-run': { path: `/p1p2/imports/${importId}/neo4j-dry-run`, label: 'Neo4j Dry Run' },
      apply: { path: `/p1p2/imports/${importId}/apply-neo4j`, label: 'Neo4j Core Apply' },
      overlay: { path: `/p1p2/imports/${importId}/generate-odoo-overlay-data`, label: 'Odoo Overlay Data' },
    }
    setBusy(true); addLog('info', `${paths[action].label}を実行します...`)
    try {
      const { res } = await tryFetch(paths[action].path, { method: 'POST' })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, `${paths[action].label} failed`))
      addLog('ok', `${paths[action].label}完了: ${data.status || data.summary?.status || 'ok'}`)
      await loadPhaseArtifacts(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
    finally { setBusy(false) }
  }



  async function loadP5InternalDesignImports(silent = false) {
    try {
      const { res } = await tryFetch('/p5/internal-design/imports')
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P5 Internal Design Pack imports load failed'))
      const items = data.items || []
      setP5InternalDesignImports(items)
      const nextId = p5SelectedInternalDesignId || items[0]?.design_import_id || ''
      if (nextId) {
        setP5SelectedInternalDesignId(nextId)
        await loadP5InternalDesignDetail(nextId, true)
      }
      if (!silent) addLog('ok', `P5 Internal Design Pack imports: ${items.length}`)
    } catch (e: any) {
      if (!silent) addLog('warn', `P5 Internal Design Pack imports not loaded: ${e?.message || String(e)}`)
    }
  }

  async function loadP5InternalDesignDetail(designImportId: string, silent = false) {
    if (!designImportId) return
    try {
      const { res: valRes } = await tryFetch(`/p5/internal-design/imports/${designImportId}/validation`)
      const val = await readJsonSafe(valRes)
      if (!valRes.ok) throw new Error(errorMessage(val, 'P5 Internal Design Pack validation load failed'))
      setP5InternalDesignValidation(val)
      const { res: prevRes } = await tryFetch(`/p5/internal-design/imports/${designImportId}/preview`)
      const prev = await readJsonSafe(prevRes)
      if (prevRes.ok) setP5InternalDesignPreview(prev)
      if (!silent) addLog(val.status === 'valid' ? 'ok' : 'warn', `P5 Internal Design Pack Validate: ${val.status} / nodes=${val.node_count || 0} / rels=${val.relationship_count || 0}`)
    } catch (e: any) {
      if (!silent) addLog('error', e?.message || String(e))
    }
  }

  async function uploadP5InternalDesignPack() {
    if (!p5InternalDesignFile) return
    const fd = new FormData()
    fd.append('file', p5InternalDesignFile)
    setBusy(true)
    setP5InternalDesignImportResult(null)
    addLog('info', `P5 Internal Design PackをImportします: ${p5InternalDesignFile.name}`)
    try {
      const { res } = await tryFetch('/p5/internal-design/import', { method: 'POST', body: fd })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P5 Internal Design Pack import failed'))
      setP5InternalDesignImportResult(data)
      const id = data.design_import_id || ''
      setP5SelectedInternalDesignId(id)
      setP5InternalDesignValidation(data.validation || null)
      addLog(data.status === 'validated' ? 'ok' : 'warn', `P5 Internal Design Pack Import: ${data.status} / ${id}`)
      await loadP5InternalDesignImports(true)
      if (id) await loadP5InternalDesignDetail(id, true)
    } catch (e: any) {
      const message = e?.message || String(e)
      setP5InternalDesignImportResult({ status: 'error', message })
      addLog('error', message)
    } finally { setBusy(false) }
  }

  async function validateP5InternalDesign() {
    if (!p5SelectedInternalDesignId) return
    setBusy(true)
    try {
      const { res } = await tryFetch(`/p5/internal-design/imports/${p5SelectedInternalDesignId}/validate`, { method: 'POST' })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, 'P5 Internal Design Pack Validate failed'))
      setP5InternalDesignValidation(data)
      addLog(data.status === 'valid' ? 'ok' : 'warn', `P5 Internal Design Pack Validate: ${data.status} / errors=${data.error_count || 0}`)
      await loadP5InternalDesignImports(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
    finally { setBusy(false) }
  }

  async function runP5InternalNeo4j(action: 'dry-run' | 'apply') {
    if (!p5SelectedInternalDesignId) return
    const path = action === 'dry-run'
      ? `/p5/internal-design/imports/${p5SelectedInternalDesignId}/neo4j-dry-run`
      : `/p5/internal-design/imports/${p5SelectedInternalDesignId}/neo4j/apply`
    setBusy(true)
    try {
      const { res } = await tryFetch(path, { method: 'POST' })
      const data = await readJsonSafe(res)
      if (!res.ok) throw new Error(errorMessage(data, `P5 Internal Neo4j ${action} failed`))
      if (action === 'dry-run') setP5Neo4jDryRunResult(data)
      else setP5Neo4jApplyResult(data)
      addLog('ok', `P5 Internal Neo4j ${action === 'dry-run' ? 'Dry Run' : 'Apply to Neo4j'}: ${data.status} / nodes=${data.node_count || 0} / rels=${data.relationship_count || 0}`)
      await loadP5InternalDesignImports(true)
    } catch (e: any) { addLog('error', e?.message || String(e)) }
    finally { setBusy(false) }
  }

  useEffect(() => {
    if (selectedPhaseKey === 'P5') loadP5InternalDesignImports(true)
    if (selectedPhaseKey === 'P6') loadP6DiagramPacks(true)
    if (selectedPhaseKey === 'P7') loadP7AuthorityPacks(true)
  }, [selectedPhaseKey])

  useEffect(() => {
    if (selectedPhaseKey === 'P7' && selectedP7AuthorityPack?.authority_import_id && p7SelectedViewKey) loadP7AuthorityView(p7SelectedViewKey)
  }, [selectedPhaseKey, selectedP7AuthorityPack?.authority_import_id, p7SelectedViewKey])

  const p2Ready = selected?.phase_key === 'P2' && !!selected.import_id
  const p3Ready = selected?.phase_key === 'P3' && !!selected.import_id
  const p2OverlayGenerated = selected?.phase_key === 'P2' && selected.status === 'odoo_overlay_data_generated'
  const p4p5Selected = selected?.phase_key === 'P4' || selected?.phase_key === 'P5'

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Odoo Inquisitor / F&amp;G Factory</p>
          <h1>Phase Matrix Console</h1>
          <p className="subtitle">P1〜P7を同じ粒度で確認します。左パネルでImport/Actionsを集約し、右パネルは選択Phaseの結果と成果物を表示します。</p>
        </div>
        <div className="top-actions">
          <button className="secondary" onClick={() => loadPhaseArtifacts()} disabled={busy}><RefreshCcw size={16}/> Refresh</button>
        </div>
      </header>

      <section className="layout phase-layout">
        <aside className="panel left-panel">
          <div className="section-title"><Upload size={17}/> P1/P2 Combined Import</div>
          <label className="upload-box small-upload">
            <input type="file" accept=".zip" onChange={(e) => setP1P2File(e.target.files?.[0] || null)} />
            <FileArchive size={22}/>
            <span>{p1p2File ? p1p2File.name : 'P1P2_GAP_AWARE_COMBINED_DATA_PACK.zip'}</span>
          </label>
          <button className="primary" onClick={uploadP1P2Pack} disabled={!p1p2File || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import P1/P2</button>

          <div className="section-title spaced"><Upload size={17}/> P3 Neo4j-first Import</div>
          <div className="left-action-box p3-only-box">
            <p className="action-hint">A. P3 Neo4j-first Import / Validate / Apply 専用です。P3にだけ紐づけ、P1/P2/P4/P5には影響させません。</p>
            <label className="upload-box small-upload">
              <input type="file" accept=".zip,.json" onChange={(e) => setP3File(e.target.files?.[0] || null)} />
              <FileArchive size={22}/>
              <span>{p3File ? p3File.name : 'P3_NEO4J_FIRST_IMPORT_PACK.zip'}</span>
            </label>
            <button className="primary p3-button" onClick={uploadP3Neo4jFirstPack} disabled={!p3File || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import P3 Neo4j-first</button>
            {p3ImportResult && (
              <div className={`inline-result ${p3ImportResult.status === 'error' || p3ImportResult.status === 'validation_failed' ? 'bad' : 'good'}`}>
                <b>P3 Import Result</b>
                <p>{p3ImportResult.message || p3ImportResult.status || 'ok'}</p>
                {p3ImportResult.import_id && <p className="mono">id: {p3ImportResult.import_id}</p>}
                {p3ImportResult.count_summary && <p>graph: {fmt(p3ImportResult.count_summary.nodes)} nodes / {fmt(p3ImportResult.count_summary.relationships)} rels</p>}
              </div>
            )}
          </div>

          <div className="section-title spaced"><Wrench size={17}/> Phase Actions</div>
          {selected?.phase_key === 'P2' ? (
            <div className="left-action-box">
              <p className="action-hint">P2はP1/P2 Combined coreに対して実行します。GAPはreport-onlyで自動反映対象外です。</p>
              <button className="secondary" onClick={() => runP2Action('repair')} disabled={busy || !p2Ready}><Wrench size={16}/> Repair Context</button>
              <button className="secondary" onClick={() => runP2Action('dry-run')} disabled={busy || !p2Ready}><Network size={16}/> Neo4j Dry Run</button>
              <button className="primary apply" onClick={() => runP2Action('apply')} disabled={busy || !p2Ready}><Network size={16}/> Apply Neo4j Core</button>
              <button className="primary odoo" onClick={() => runP2Action('overlay')} disabled={busy || !p2Ready}><PackageOpen size={16}/> Generate Overlay Data</button>
              <button className="secondary" onClick={exportPromptPack} disabled={busy || !p2Ready || !p2OverlayGenerated}><FileArchive size={16}/> Export ChatGPT Prompt Pack</button>
            </div>
          ) : selected?.phase_key === 'P3' ? (
            <div className="left-action-box p3-only-box">
              <p className="action-hint">P3はAでNeo4j-firstを反映し、Bで候補を機械分類し、CでAddon Inputを生成し、DでValidateし、EでChatGPTコード生成用Material PackをExportします。FのOdooコードImportは、生成済みコードだけでなく、Usability Enhancementや修正addon ZIPも同じ入口でImport/Validateします。ApplyはGで別途行います。</p>
              <button className="secondary" onClick={() => runP3Action('dry-run')} disabled={busy || !p3Ready}><Network size={16}/> P3 Neo4j Dry Run</button>
              <button className="primary apply p3-button" onClick={() => runP3Action('apply')} disabled={busy || !p3Ready}><Network size={16}/> Apply P3 Neo4j</button>
              <button className="secondary p3-button" onClick={() => runP3Action('inspect')} disabled={busy || !p3Ready}><CheckCircle2 size={16}/> Generate B Inspection</button>
              <button className="primary odoo p3-button" onClick={() => runP3Action('addon')} disabled={busy || !p3Ready}><PackageOpen size={16}/> Generate C Addon Input</button>
              <button className="secondary p3-button" onClick={() => runP3Action('validate')} disabled={busy || !p3Ready}><CheckCircle2 size={16}/> Validate D Addon Input</button>
              <button className="primary odoo p3-button" onClick={() => runP3Action('codegen')} disabled={busy || !p3Ready}><FileArchive size={16}/> Export E Codegen Material Pack</button>
              <label className="upload-row small">
                <input type="file" accept=".zip" onChange={(e) => setP3GeneratedCodeFile(e.target.files?.[0] || null)} />
                <span>{p3GeneratedCodeFile ? p3GeneratedCodeFile.name : 'Generated / Enhancement / Patch Odoo addon ZIP'}</span>
              </label>
              <button className="secondary p3-button" onClick={uploadP3GeneratedOdooCodePack} disabled={busy || !p3Ready || !p3GeneratedCodeFile}><Upload size={16}/> Import F Odoo Code ZIP</button>
              <button className="primary apply p3-button" onClick={applyP3OdooAddonDirect} disabled={busy || !p3Ready}><PackageOpen size={16}/> Apply G Odoo Addon Direct</button>
              {p3InspectionResult && (
                <div className={`inline-result ${p3InspectionResult.status === 'inspection_requires_review' ? 'warn' : 'good'}`}>
                  <b>B Inspection</b>
                  <p>{p3InspectionResult.status || 'ok'}</p>
                  {p3InspectionResult.summary && <p>include: {fmt(p3InspectionResult.summary.include_candidate_count)} / support masters: {fmt(p3InspectionResult.summary.support_master_count)} / issues: {fmt(p3InspectionResult.summary.support_link_issue_count)}</p>}
                </div>
              )}
              {p3AddonInputResult && (
                <div className={`inline-result ${p3AddonInputResult.status === 'p3_addon_input_generated' ? 'good' : 'warn'}`}>
                  <b>C Addon Input</b>
                  <p>{p3AddonInputResult.status || 'ok'}</p>
                  {p3AddonInputResult.summary && <p>masters: {fmt(p3AddonInputResult.summary.master_definition_count)} / fields: {fmt(p3AddonInputResult.summary.field_definition_count)} / views: {fmt(p3AddonInputResult.summary.view_placement_count)}</p>}
                  {p3AddonInputResult.download_url && <p><a href={downloadHref(apiBaseUsed, p3AddonInputResult.download_url)} target="_blank" rel="noreferrer">Download P3 Addon Input ZIP</a></p>}
                </div>
              )}
              {p3AddonInputValidationResult && (
                <div className={`inline-result ${p3AddonInputValidationResult.valid ? 'good' : 'bad'}`}>
                  <b>D Addon Input Validate</b>
                  <p>{p3AddonInputValidationResult.status || 'ok'}</p>
                  {p3AddonInputValidationResult.summary && <p>errors: {fmt(p3AddonInputValidationResult.summary.error_count)} / warnings: {fmt(p3AddonInputValidationResult.summary.warning_count)} / fields: {fmt(p3AddonInputValidationResult.summary.field_definition_count)}</p>}
                  <p className="action-hint">Cで保存済みのp3_addon_input.jsonを検証しています。同じZIPの再Importは不要です。</p>
                </div>
              )}
              {p3CodegenMaterialResult && (
                <div className={`inline-result ${p3CodegenMaterialResult.status === 'p3_codegen_material_pack_exported' ? 'good' : 'warn'}`}>
                  <b>E Codegen Material Pack</b>
                  <p>{p3CodegenMaterialResult.status || 'ok'}</p>
                  {p3CodegenMaterialResult.summary && <p>masters: {fmt(p3CodegenMaterialResult.summary.master_definition_count)} / fields: {fmt(p3CodegenMaterialResult.summary.field_definition_count)} / warnings: {fmt(p3CodegenMaterialResult.validation_gate?.warning_count || 0)}</p>}
                  {p3CodegenMaterialResult.download_url && <p><a href={downloadHref(apiBaseUsed, p3CodegenMaterialResult.download_url)} target="_blank" rel="noreferrer">Download P3 Codegen Material Pack</a></p>}
                </div>
              )}
              {p3GeneratedCodeImportResult && (
                <div className={`inline-result ${p3GeneratedCodeImportResult.valid ? 'good' : 'bad'}`}>
                  <b>F Odoo Code ZIP Import / Validate</b>
                  <p>{p3GeneratedCodeImportResult.status || 'ok'}</p>
                  {p3GeneratedCodeImportResult.summary && <p>addons: {fmt(p3GeneratedCodeImportResult.summary.addon_count)} / py: {fmt(p3GeneratedCodeImportResult.summary.python_file_count)} / xml: {fmt(p3GeneratedCodeImportResult.summary.xml_file_count)} / errors: {fmt(p3GeneratedCodeImportResult.summary.error_count)}</p>}
                  <p className="action-hint">このImportはP3生成コード、Demo Usability Enhancement、今後のOdoo addonコード修正ZIPの共通入口です。ApplyはGで別工程です。</p>
                </div>
              )}
              {p3OdooApplyResult && (
                <div className={`inline-result ${p3OdooApplyResult.status === 'odoo_addon_direct_applied' ? 'good' : 'bad'}`}>
                  <b>G Apply Odoo Addon Direct</b>
                  <p>{p3OdooApplyResult.message || p3OdooApplyResult.status || 'ok'}</p>
                  {p3OdooApplyResult.applied_addon_count !== undefined && <p>addons: {fmt(p3OdooApplyResult.applied_addon_count)} / target: <span className="mono">{p3OdooApplyResult.target_root}</span></p>}
                  <p className="action-hint">Addonをextra-addons相当の配置先へ反映しました。Odoo側ではApps更新後、InstallまたはUpgradeを実行してください。</p>
                </div>
              )}
            </div>
          ) : p4p5Selected ? (
            <div className="left-action-box p4p5-left-hint">
              <p className="action-hint">P4/P5は統合してDevelopment Theme Catalogとして扱います。左のPhase StatusでP4またはP5を選ぶと、中央にTheme一覧、右にKey Exportが表示されます。</p>
              <p className="action-hint">Importは中央上部の1ボタンからCatalog ZIP/JSON全体を取り込み、自動Validateします。</p>
              {selected?.phase_key === 'P5' && (
                <div className="p5-internal-design-box">
                  <div className="section-title compact-title"><Upload size={16}/> P5 Internal Design Pack</div>
                  <p className="action-hint">ChatGPTで生成したInternal Design PackをImportし、Validate後にNeo4j Dry Run / Apply to Neo4jまで実行します。</p>
                  <label className="upload-box small-upload">
                    <input type="file" accept=".zip" onChange={(e) => setP5InternalDesignFile(e.target.files?.[0] || null)} />
                    <FileArchive size={20}/>
                    <span>{p5InternalDesignFile ? p5InternalDesignFile.name : 'P4P5_INTERNAL_DESIGN_OUTPUT_PACK.zip'}</span>
                  </label>
                  <button className="primary p5-button" onClick={uploadP5InternalDesignPack} disabled={!p5InternalDesignFile || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import Pack</button>
                  {p5InternalDesignImports.length > 0 && (
                    <select className="select-input" value={p5SelectedInternalDesignId || selectedP5InternalDesign?.design_import_id || ''} onChange={(e) => { setP5SelectedInternalDesignId(e.target.value); loadP5InternalDesignDetail(e.target.value) }}>
                      {p5InternalDesignImports.map((x) => <option key={x.design_import_id} value={x.design_import_id}>{x.design_import_id} / {x.status}</option>)}
                    </select>
                  )}
                  <div className="p5-internal-button-row">
                    <button className="secondary" onClick={validateP5InternalDesign} disabled={!p5SelectedInternalDesignId || busy}><CheckCircle2 size={15}/> Validate Pack</button>
                    <button className="secondary" onClick={() => runP5InternalNeo4j('dry-run')} disabled={!p5SelectedInternalDesignId || busy || p5InternalDesignValidation?.status !== 'valid'}><Network size={15}/> Neo4j Dry Run</button>
                    <button className="primary apply" onClick={() => runP5InternalNeo4j('apply')} disabled={!p5SelectedInternalDesignId || busy || p5InternalDesignValidation?.status !== 'valid'}><Network size={15}/> Apply to Neo4j</button>
                  </div>
                  {p5InternalDesignValidation && <div className={`inline-result ${p5InternalDesignValidation.status === 'valid' ? 'good' : 'bad'}`}>
                    <b>Internal Design Validation</b>
                    <p>{p5InternalDesignValidation.status} / errors {p5InternalDesignValidation.error_count || 0} / warnings {p5InternalDesignValidation.warning_count || 0}</p>
                    <p>graph: {fmt(p5InternalDesignValidation.node_count || 0)} nodes / {fmt(p5InternalDesignValidation.relationship_count || 0)} rels</p>
                  </div>}
                  {p5Neo4jDryRunResult && <div className="inline-result good"><b>Neo4j Dry Run Result</b><p>{p5Neo4jDryRunResult.status} / {fmt(p5Neo4jDryRunResult.node_count)} nodes / {fmt(p5Neo4jDryRunResult.relationship_count)} rels</p></div>}
                  {p5Neo4jApplyResult && <div className="inline-result good"><b>Apply to Neo4j Result</b><p>{p5Neo4jApplyResult.status} / applied {fmt(p5Neo4jApplyResult.applied_node_count)} nodes / {fmt(p5Neo4jApplyResult.applied_relationship_count)} rels</p></div>}
                </div>
              )}
            </div>
          ) : selected?.phase_key === 'P6' ? (
            <div className="left-action-box muted-box">
              <p className="action-hint">P6は図示PACKのImport / Validate / Download専用です。今回の対象はP3までのER図PACKです。操作ボタンは中央パネルに表示します。</p>
            </div>
          ) : (
            <div className="left-action-box muted-box">
              <p className="action-hint">{selected?.phase_key} はこのステップではRead/Downloadのみです。Phase別Import/Actionは後続で追加します。</p>
            </div>
          )}

          <div className="section-title spaced"><PackageOpen size={17}/> Addon Input / Direct Odoo Apply</div>
          <div className="left-action-box">
            <p className="action-hint">ChatGPTで作成した P1P2_ODOO_ADDON_INPUT_CANDIDATE.zip をImportし、検証OKなら custom_addons へ直接反映します。</p>
            <label className="upload-box small-upload">
              <input type="file" accept=".zip" onChange={(e) => setAddonInputFile(e.target.files?.[0] || null)} />
              <FileArchive size={20}/>
              <span>{addonInputFile ? addonInputFile.name : 'P1P2_ODOO_ADDON_INPUT_CANDIDATE.zip'}</span>
            </label>
            <button className="secondary" onClick={uploadAddonInputCandidate} disabled={!addonInputFile || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import Addon Input</button>
            <button className="primary odoo" onClick={applyOdooAddonDirect} disabled={busy || !selectedAddonInput || selectedAddonInput.validation_status !== 'ok'}>{busy ? <Loader2 className="spin" size={16}/> : <PackageOpen size={16}/>} Apply Odoo Addon Direct</button>
            {selectedAddonInput && <p className="action-hint">Selected: {selectedAddonInput.addon_name || 'addon'} / {selectedAddonInput.validation_status || selectedAddonInput.status || '-'}</p>}
            {addonInputResult && (
              <div className={`inline-result ${addonInputResult.status === 'error' || addonInputResult.validation_status === 'failed' ? 'bad' : 'good'}`}>
                <b>Import Result</b>
                <p>{addonInputResult.message || addonInputResult.status || addonInputResult.validation_status || 'ok'}</p>
                {addonInputResult.total_records != null && <p>records: {fmt(addonInputResult.total_records)}</p>}
                {addonInputResult.addon_input_id && <p className="mono">id: {addonInputResult.addon_input_id}</p>}
              </div>
            )}
            {addonApplyResult && (
              <div className={`inline-result ${addonApplyResult.status === 'error' ? 'bad' : 'good'}`}>
                <b>Apply Result</b>
                <p>{addonApplyResult.message || addonApplyResult.status || 'ok'}</p>
                {addonApplyResult.addon_path && <p className="mono">{addonApplyResult.addon_path}</p>}
              </div>
            )}
          </div>

          <div className="section-title spaced">Phase Status</div>
          <div className="phase-stack">
            {phases.map((p) => (
              <button key={p.phase_key} className={`phase-card ${selectedPhaseKey === p.phase_key ? 'active' : ''}`} onClick={() => setSelectedPhaseKey(p.phase_key)}>
                <div><b>{p.phase_key}</b><span>{p.label || phaseLabels[p.phase_key]}</span><small>{p.artifact_count ? `${p.artifact_count} artifacts` : 'No artifacts'}</small></div>
                <Pill status={p.status}/>
              </button>
            ))}
          </div>
        </aside>

        {p4p5Selected ? (
          <P4P5EmbeddedConsole />
        ) : selectedPhaseKey === 'P7' ? (
          <>
            <section className="panel center-panel p7-center-panel">
              <div className="section-title"><Network size={17}/> P7 Authority / Organization yFiles Console</div>
              <p className="info-note">P7は権限・組織・承認・データ可視範囲を扱う独立レイヤです。Import / Validate / yFiles Payload表示 / Downloadのみを行い、Odoo CodegenやApplyは実行しません。</p>
              <div className="detail-card">
                <h3>Authority Visualization Pack Import</h3>
                <label className="upload-box small-upload">
                  <input type="file" accept=".zip" onChange={(e) => setP7AuthorityFile(e.target.files?.[0] || null)} />
                  <FileArchive size={22}/>
                  <span>{p7AuthorityFile ? p7AuthorityFile.name : 'SAMPLECO_P6_AUTHORITY_VISUALIZATION_PACK_v1.zip'}</span>
                </label>
                <div className="button-row wrap">
                  <button className="primary" onClick={uploadP7AuthorityPack} disabled={!p7AuthorityFile || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import Authority Pack</button>
                  <button className="secondary" onClick={validateP7AuthorityPack} disabled={!selectedP7AuthorityPack || busy}><CheckCircle2 size={16}/> Validate Imported Pack</button>
                  <button className="secondary" onClick={() => loadP7AuthorityPacks()} disabled={busy}><RefreshCcw size={16}/> Refresh Packs</button>
                </div>
                {p7ImportResult && (
                  <div className={`inline-result ${p7ImportResult.status === 'error' || p7ImportResult.status === 'validation_failed' ? 'bad' : 'good'}`}>
                    <b>Import Result</b>
                    <p>{p7ImportResult.message || p7ImportResult.status || 'ok'}</p>
                    {p7ImportResult.authority_import_id && <p className="mono">id: {p7ImportResult.authority_import_id}</p>}
                    {p7ImportResult.summary && <p>views: {fmt(p7ImportResult.summary.view_count)} / nodes: {fmt(p7ImportResult.summary.node_count)} / edges: {fmt(p7ImportResult.summary.edge_count)} / codegen-ready: {fmt(p7ImportResult.summary.codegen_ready_count)}</p>}
                  </div>
                )}
                {p7ValidationResult && (
                  <div className={`inline-result ${p7ValidationResult.valid ? 'good' : 'bad'}`}>
                    <b>Validation Result</b>
                    <p>{p7ValidationResult.status || 'ok'} / errors: {fmt(p7ValidationResult.error_count)} / warnings: {fmt(p7ValidationResult.warning_count)}</p>
                    {p7ValidationResult.warnings?.length ? <pre className="mini-pre">{p7ValidationResult.warnings.slice(0, 8).join('\n')}</pre> : null}
                    {p7ValidationResult.errors?.length ? <pre className="error-box">{p7ValidationResult.errors.slice(0, 8).join('\n')}</pre> : null}
                  </div>
                )}
              </div>

              {selectedP7AuthorityPack ? (
                <div className="detail-card p7-view-card">
                  <div className="section-title"><Network size={17}/> yFiles View Selector</div>
                  <div className="button-row wrap">
                    <select className="select-input" value={selectedP7AuthorityPack.authority_import_id || ''} onChange={(e) => setP7SelectedAuthorityId(e.target.value)}>
                      {p7AuthorityPacks.map((x) => <option key={x.authority_import_id} value={x.authority_import_id}>{x.filename || x.authority_import_id} / {x.status}</option>)}
                    </select>
                    <select className="select-input" value={p7SelectedViewKey} onChange={(e) => setP7SelectedViewKey(e.target.value)}>
                      {(selectedP7AuthorityPack.views || []).map((v: any) => <option key={v.view_key} value={v.view_key}>{v.title_ja || v.view_key} / {fmt(v.node_count)} nodes</option>)}
                    </select>
                    <button className="secondary" onClick={() => loadP7AuthorityView()} disabled={busy || !selectedP7AuthorityPack}><RefreshCcw size={16}/> Reload View</button>
                    <a className="download-link" href={downloadHref(apiBaseUsed, `/p7/authority-packs/${selectedP7AuthorityPack.authority_import_id}/views/${encodeURIComponent(p7SelectedViewKey)}/download`)} target="_blank" rel="noreferrer"><Download size={15}/> Download View JSON</a>
                  </div>
                  <AuthorityGraphViewer payload={p7ViewPayload} selected={p7SelectedElement} onSelect={setP7SelectedElement}/>
                </div>
              ) : <p className="empty">Authority Visualization PackをImportすると、組織・承認・可視範囲のyFiles Payloadを選択表示できます。</p>}
            </section>

            <aside className="panel right-panel phase-action-panel">
              <div className="section-title"><CheckCircle2 size={17}/> P7 Result</div>
              <div className="status-banner good-bg"><CheckCircle2 size={18}/> Authority Visualization Console</div>
              {selectedP7AuthorityPack ? <div className="detail-card">
                <h3>{selectedP7AuthorityPack.filename || 'Imported Authority Pack'}</h3>
                <p><b>Status:</b> {statusLabel(selectedP7AuthorityPack.status)}</p>
                <p><b>Mode:</b> Import / Validate / yFiles View / Download only</p>
                <p><b>Current View:</b> {p7ViewPayload?.title_ja || p7SelectedViewKey}</p>
                {selectedP7AuthorityPack.summary && <div className="summary-strip compact vertical-metrics">
                  <Metric label="Views" value={selectedP7AuthorityPack.summary.view_count || 0}/>
                  <Metric label="Nodes" value={selectedP7AuthorityPack.summary.node_count || 0}/>
                  <Metric label="Edges" value={selectedP7AuthorityPack.summary.edge_count || 0}/>
                  <Metric label="Approval" value={selectedP7AuthorityPack.summary.approval_process_count || 0}/>
                  <Metric label="Codegen-ready" value={selectedP7AuthorityPack.summary.codegen_ready_count || 0} tone="warn"/>
                </div>}
              </div> : <p className="empty">まだP7 PackがImportされていません。</p>}
              {p7SelectedElement && <div className="detail-card spaced">
                <h3>Selected Element</h3>
                <p><b>ID:</b> {p7SelectedElement.id}</p>
                <p><b>Type:</b> {p7SelectedElement.type}</p>
                <p><b>Label:</b> {p7SelectedElement.label || '-'}</p>
                <pre className="mini-pre">{JSON.stringify(p7SelectedElement.data || p7SelectedElement, null, 2)}</pre>
              </div>}
              <div className="section-title spaced"><Download size={17}/> Downloads</div>
              <div className="download-stack">
                {(selectedP7AuthorityPack?.actions || []).map((a: any) => <a key={a.action_key} className="download-link action-download" href={downloadHref(apiBaseUsed, a.download_url)} target="_blank" rel="noreferrer"><Download size={15}/><span><b>{a.label_ja || a.action_key}</b><small>{a.group_label_ja || a.group_key}</small></span></a>)}
              </div>
              <div className="result-note">
                <p>P7ではOdoo Codegen / Odoo Apply / Neo4j Applyは実行しません。ready_for_codegenの承認プロセスは表示・DLのみです。</p>
              </div>
            </aside>
          </>
        ) : selectedPhaseKey === 'P6' ? (
          <>
            <section className="panel center-panel">
              <div className="section-title"><Network size={17}/> P6 Diagram Pack Console</div>
              <p className="info-note">このP6はP3までの成果物とOdoo DB抽出情報から作成したER図PACKだけを扱います。P4/P5の顧客回答・詳細ロジック・最終Odoo反映結果は含みません。</p>
              <div className="detail-card">
                <h3>P3 Diagram Pack Import</h3>
                <label className="upload-box small-upload">
                  <input type="file" accept=".zip" onChange={(e) => setP6DiagramFile(e.target.files?.[0] || null)} />
                  <FileArchive size={22}/>
                  <span>{p6DiagramFile ? p6DiagramFile.name : 'P3_DIAGRAM_DATA_PACK_v1.zip'}</span>
                </label>
                <div className="button-row wrap">
                  <button className="primary" onClick={uploadP6DiagramPack} disabled={!p6DiagramFile || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import P3 Diagram Pack</button>
                  <button className="secondary" onClick={validateP6DiagramPack} disabled={!selectedP6DiagramPack || busy}><CheckCircle2 size={16}/> Validate Imported Pack</button>
                  <button className="secondary" onClick={() => loadP6DiagramPacks()} disabled={busy}><RefreshCcw size={16}/> Refresh Packs</button>
                </div>
                {p6ImportResult && (
                  <div className={`inline-result ${p6ImportResult.status === 'error' || p6ImportResult.status === 'validation_failed' ? 'bad' : 'good'}`}>
                    <b>Import Result</b>
                    <p>{p6ImportResult.message || p6ImportResult.status || 'ok'}</p>
                    {p6ImportResult.diagram_import_id && <p className="mono">id: {p6ImportResult.diagram_import_id}</p>}
                    {p6ImportResult.summary && <p>actions: {fmt(p6ImportResult.summary.action_count)} / graphs: {fmt(p6ImportResult.summary.graph_file_count)} / missing models: {fmt(p6ImportResult.summary.missing_model_count)}</p>}
                  </div>
                )}
                {p6ValidationResult && (
                  <div className={`inline-result ${p6ValidationResult.valid ? 'good' : 'bad'}`}>
                    <b>Validation Result</b>
                    <p>{p6ValidationResult.status || 'ok'} / errors: {fmt(p6ValidationResult.error_count)} / warnings: {fmt(p6ValidationResult.warning_count)}</p>
                    {p6ValidationResult.warnings?.length ? <pre className="mini-pre">{p6ValidationResult.warnings.slice(0, 8).join('\\n')}</pre> : null}
                    {p6ValidationResult.errors?.length ? <pre className="error-box">{p6ValidationResult.errors.slice(0, 8).join('\\n')}</pre> : null}
                  </div>
                )}
              </div>

              <div className="detail-card">
                <h3>P3 Internal Structural Binding Import</h3>
                <p className="info-note">P4P5 Theme Catalogは既にシステムへImport済みという前提です。ここでは、P4質問PACK生成時に参照する内部用P3構造Bindingだけを取り込みます。ファイルを直接読むのではなく、Import済みデータを後続処理が参照します。</p>
                <label className="upload-box small-upload">
                  <input type="file" accept=".zip" onChange={(e) => setP3InternalBindingFile(e.target.files?.[0] || null)} />
                  <FileArchive size={22}/>
                  <span>{p3InternalBindingFile ? p3InternalBindingFile.name : 'P3_INTERNAL_STRUCTURAL_BINDING_PACK_v1.zip'}</span>
                </label>
                <div className="button-row wrap">
                  <button className="primary" onClick={uploadP3InternalBinding} disabled={!p3InternalBindingFile || busy}>{busy ? <Loader2 className="spin" size={16}/> : <Upload size={16}/>} Import P3 Internal Binding</button>
                  <button className="secondary" onClick={validateP3InternalBinding} disabled={!selectedP3InternalBinding || busy}><CheckCircle2 size={16}/> Validate Binding</button>
                  <button className="secondary" onClick={() => loadP3InternalBindings()} disabled={busy}><RefreshCcw size={16}/> Refresh Bindings</button>
                </div>
                {p3InternalBindingImportResult && (
                  <div className={`inline-result ${p3InternalBindingImportResult.status === 'error' || p3InternalBindingImportResult.status === 'validation_failed' ? 'bad' : 'good'}`}>
                    <b>Internal Binding Import Result</b>
                    <p>{p3InternalBindingImportResult.message || p3InternalBindingImportResult.status || 'ok'}</p>
                    {p3InternalBindingImportResult.binding_import_id && <p className="mono">id: {p3InternalBindingImportResult.binding_import_id}</p>}
                    {p3InternalBindingImportResult.counts && <p>nodes: {fmt(p3InternalBindingImportResult.counts.nodes)} / edges: {fmt(p3InternalBindingImportResult.counts.edges)} / fields: {fmt(p3InternalBindingImportResult.counts.fields)} / missing models: {fmt(p3InternalBindingImportResult.counts.missing_models)}</p>}
                  </div>
                )}
                {p3InternalBindingValidationResult && (
                  <div className={`inline-result ${p3InternalBindingValidationResult.valid ? 'good' : 'bad'}`}>
                    <b>Internal Binding Validation Result</b>
                    <p>{p3InternalBindingValidationResult.status || 'ok'} / errors: {fmt(p3InternalBindingValidationResult.error_count)} / warnings: {fmt(p3InternalBindingValidationResult.warning_count)}</p>
                    {p3InternalBindingValidationResult.warnings?.length ? <pre className="mini-pre">{p3InternalBindingValidationResult.warnings.slice(0, 8).join('\n')}</pre> : null}
                    {p3InternalBindingValidationResult.errors?.length ? <pre className="error-box">{p3InternalBindingValidationResult.errors.slice(0, 8).join('\n')}</pre> : null}
                  </div>
                )}
                {selectedP3InternalBinding && (
                  <div className="summary-strip compact">
                    <Metric label="Binding Nodes" value={selectedP3InternalBinding.counts?.nodes || 0}/>
                    <Metric label="Edges" value={selectedP3InternalBinding.counts?.edges || 0}/>
                    <Metric label="Fields" value={selectedP3InternalBinding.counts?.fields || 0}/>
                    <Metric label="Apps" value={selectedP3InternalBinding.counts?.app_keys || 0}/>
                    <Metric label="Missing" value={selectedP3InternalBinding.counts?.missing_models || 0} tone={selectedP3InternalBinding.counts?.missing_models ? 'warn' : ''}/>
                  </div>
                )}
                {p3InternalBindings.length > 1 && (
                  <select className="select-input" value={selectedP3InternalBinding?.binding_import_id || ''} onChange={(e) => setSelectedP3InternalBindingId(e.target.value)}>
                    {p3InternalBindings.map((x) => <option key={x.binding_import_id} value={x.binding_import_id}>{x.filename || x.binding_import_id} / {x.status}</option>)}
                  </select>
                )}
              </div>

              <div className="section-title spaced"><Download size={17}/> Dynamic ER Downloads</div>
              {selectedP6DiagramPack ? (
                <div className="detail-card">
                  <h3>{selectedP6DiagramPack.filename || 'Imported Diagram Pack'}</h3>
                  <p><b>Scope:</b> P3まで / 3-hop / ER</p>
                  <p><b>Status:</b> {statusLabel(selectedP6DiagramPack.status)}</p>
                  {selectedP6DiagramPack.summary && <div className="summary-strip compact">
                    <Metric label="Actions" value={selectedP6DiagramPack.summary.action_count || 0}/>
                    <Metric label="Graphs" value={selectedP6DiagramPack.summary.graph_file_count || 0}/>
                    <Metric label="Nodes" value={selectedP6DiagramPack.summary.node_count || 0}/>
                    <Metric label="Edges" value={selectedP6DiagramPack.summary.edge_count || 0}/>
                    <Metric label="Missing Models" value={selectedP6DiagramPack.summary.missing_model_count || 0} tone={selectedP6DiagramPack.summary.missing_model_count ? 'warn' : ''}/>
                  </div>}
                  {p6DiagramPacks.length > 1 && (
                    <select className="select-input" value={selectedP6DiagramPack.diagram_import_id || ''} onChange={(e) => setP6SelectedPackId(e.target.value)}>
                      {p6DiagramPacks.map((x) => <option key={x.diagram_import_id} value={x.diagram_import_id}>{x.filename || x.diagram_import_id} / {x.status}</option>)}
                    </select>
                  )}
                  <div className="diagram-action-groups">
                    {Object.entries((selectedP6DiagramPack.actions || []).reduce((acc: any, a: any) => { const k = a.group_key || 'other'; (acc[k] ||= []).push(a); return acc }, {})).map(([groupKey, actions]: any) => (
                      <div key={groupKey} className="diagram-action-group">
                        <h4>{actions[0]?.group_label_ja || groupKey}</h4>
                        <div className="button-grid">
                          {actions.map((a: any) => (
                            <a key={a.action_key} className="download-link action-download" href={downloadHref(apiBaseUsed, a.download_url)} target="_blank" rel="noreferrer">
                              <Download size={15}/>
                              <span><b>{a.label_ja || a.action_key}</b><small>{a.badge || 'P3まで'}</small></span>
                            </a>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : <p className="empty">P3 Diagram PackをImportすると、ZIP内のindexからER図DLボタンを動的生成します。</p>}
            </section>

            <aside className="panel right-panel phase-action-panel">
              <div className="section-title"><CheckCircle2 size={17}/> P6 Result</div>
              <div className="status-banner good-bg"><CheckCircle2 size={18}/> Diagram Pack Console</div>
              <div className="detail-card">
                <h3>P6: Diagrams</h3>
                <p><b>Mode:</b> Import / Validate / Download only</p>
                <p><b>Current scope:</b> P3 ER diagrams</p>
                <p><b>Imported packs:</b> {fmt(p6DiagramPacks.length)}</p>
                <p><b>P3 internal bindings:</b> {fmt(p3InternalBindings.length)}</p>
              </div>
              <div className="result-note">
                <p>P6ではNeo4j Apply / Odoo Applyは行いません。今回のP3 ER図は、P3までの成果物とOdoo DB抽出情報を元にした確認用データです。</p>
              </div>
            </aside>
          </>
        ) : (
          <>
            <section className="panel center-panel">
              <div className="section-title">P1〜P7 Phase Matrix</div>
              <div className="phase-matrix-card">
                <div className="phase-matrix-head">
                  <div>Phase</div><div>Status</div><div>Core</div><div>GAP</div><div>Artifacts</div>
                </div>
                {phases.map((p) => (
                  <button key={p.phase_key} className={`phase-matrix-row ${selectedPhaseKey === p.phase_key ? 'selected' : ''}`} onClick={() => setSelectedPhaseKey(p.phase_key)}>
                    <div className="phase-name"><b>{p.phase_key}</b><span>{p.label || phaseLabels[p.phase_key]}</span></div>
                    <div><Pill status={p.status}/></div>
                    <div>{fmt(p.core_nodes)} nodes<br/><small>{fmt(p.core_relationships)} rels</small></div>
                    <div>{fmt(p.gap_entries)} GAP<br/><small>{fmt(p.skipped_relationships)} skipped rels</small></div>
                    <div>{fmt(p.artifact_count)}</div>
                  </button>
                ))}
              </div>

              <div className="section-title spaced">Selected Phase Summary</div>
              <div className="summary-strip">
                <Metric label="Core Nodes" value={selected?.core_nodes || 0}/>
                <Metric label="Core Rels" value={selected?.core_relationships || 0}/>
                <Metric label="GAP Items" value={selected?.gap_entries || 0} tone={selected?.gap_entries ? 'warn' : ''}/>
                <Metric label="Artifacts" value={selected?.artifact_count || 0}/>
              </div>
              {selected?.phase_key === 'P2' && selected.gap_entries > 0 && (
                <p className="info-note">P2 GAPはOdoo自動生成対象から除外し、F&amp;Gレポート用に保持しています。</p>
              )}
              {selected?.phase_key === 'P3' && (
                <p className="info-note">P3は A. Neo4j-first Import / Validate / Apply、B. Burn-in Candidate Inspection、C. Addon Input生成、D. Addon Input Validate、E. Codegen Material Pack Export、F. Odoo Code ZIP Import / Validate、G. Apply Odoo Addon Directまで対応します。</p>
              )}
            </section>

            <aside className="panel right-panel phase-action-panel">
              <div className="section-title"><CheckCircle2 size={17}/> {selected?.phase_key} Result</div>
              <div className={`status-banner ${statusTone(selected?.status) === 'good' ? 'good-bg' : statusTone(selected?.status) === 'bad' ? 'bad-bg' : ''}`}>
                {statusTone(selected?.status) === 'bad' ? <AlertTriangle size={18}/> : <CheckCircle2 size={18}/>} {statusLabel(selected?.status)}
              </div>
              <div className="detail-card">
                <h3>{selected?.phase_key}: {selected?.label}</h3>
                <p><b>Import ID:</b> {selected?.import_id || '-'}</p>
                <p><b>Core:</b> {fmt(selected?.core_nodes)} nodes / {fmt(selected?.core_relationships)} rels</p>
                <p><b>GAP:</b> {fmt(selected?.gap_entries)} detected / {fmt(selected?.skipped_relationships)} rels skipped</p>
              </div>

              <div className="result-note">
                <p>このパネルは選択Phaseの結果表示です。Importと実行操作は左パネルに集約しています。</p>
                {selected?.phase_key === 'P2' && <p>F&amp;G GAPはOdoo自動生成対象から除外し、GAPレポートとして保持します。</p>}
                {selected?.phase_key === 'P3' && <p>P3のImport結果、Burn-in Inspection、Addon Input生成、Addon Input Validate、Codegen Material Pack Export結果はP3専用成果物として保存されます。他フェーズのImport状態やArtifactには混在させません。</p>}
              </div>

              {selected?.phase_key === 'P2' && (
                <div className="detail-card addon-result-card">
                  <h3>Addon Mapping / Direct Apply</h3>
                  <p><b>Prompt Pack:</b> {promptPackResult ? 'Generated' : 'Not generated or not loaded'}</p>
                  {promptPackResult?.download_url && <p><a href={downloadHref(apiBaseUsed, promptPackResult.download_url)} target="_blank" rel="noreferrer">Download ChatGPT Prompt Pack</a></p>}
                  <p><b>Addon Inputs:</b> {addonInputs.length}</p>
                  {addonInputs.length > 0 && (
                    <select className="select-input" value={selectedAddonInput?.addon_input_id || ''} onChange={(e) => setSelectedAddonInputId(e.target.value)}>
                      {addonInputs.map((x) => <option key={x.addon_input_id} value={x.addon_input_id}>{x.addon_name || 'addon'} / {x.validation_status || x.status || '-'} / {x.total_records || 0} records</option>)}
                    </select>
                  )}
                  {selectedAddonInput && (
                    <div className="mini-list">
                      <p><b>Input ID:</b> {selectedAddonInput.addon_input_id}</p>
                      <p><b>Addon:</b> {selectedAddonInput.addon_name || '-'} / {selectedAddonInput.display_name || '-'}</p>
                      <p><b>Records:</b> {fmt(selectedAddonInput.total_records || 0)}</p>
                      <p><b>Apply:</b> {selectedAddonInput.apply_status || '-'}</p>
                      {selectedAddonInput.addon_path && <p><b>Path:</b> {selectedAddonInput.addon_path}</p>}
                    </div>
                  )}
                </div>
              )}

              <div className="section-title spaced"><Download size={17}/> Downloads</div>
              <div className="artifact-list">
                {selectedArtifacts.length ? selectedArtifacts.map((a) => (
                  <a key={a.artifact_id} className="artifact-row" href={downloadHref(apiBaseUsed, a.download_url)} target="_blank" rel="noreferrer">
                    <span><b>{a.name}</b><small>{artifactKindLabel(a.kind)} / {bytes(a.size_bytes)}</small></span>
                    <Download size={15}/>
                  </a>
                )) : <p className="empty">このPhaseの成果物はまだありません。</p>}
              </div>
              {selected?.phase_key === 'P2' && p2OverlayGenerated && <p className="info-note">Overlay ZIP / core payload / GAP report をここから取得できます。</p>}
            </aside>
          </>
        )}
      </section>

      <section className="panel log-panel"><div className="section-title">Run Log</div><pre>{logs.map((l) => `${l.level === 'ok' ? '◎' : l.level === 'warn' ? '⚠' : l.level === 'error' ? '✕' : '○'} ${l.text}`).join('\n')}</pre></section>
    </main>
  )
}


type P4P5ImportSummary = {
  import_id: string
  filename?: string
  status?: string
  theme_count?: number
  app_count?: number
  app_counts?: Record<string, number>
  total_question_count?: number
  total_hypothesis_item_count?: number
  total_scenario_count?: number
  p3_context_theme_count?: number
  customer_pack_ready_theme_count?: number
  links?: Record<string, string>
}

type P4P5ThemeRow = {
  development_theme_key: string
  title_ja: string
  summary_ja?: string
  app_key: string
  target_apps?: string[]
  business_domain?: string
  process_stage?: string
  implementation_pattern?: string
  risk_level?: string
  codegen_readiness?: string
  customer_answer_status?: string
  answered_count?: number
  answer_total_count?: number
  answer_import_id?: string
  answered_pack_filename?: string
  answered_at?: string
  has_customization_definition?: boolean
  customization_title_ja?: string
  internal_design_export_status?: string
  internal_design_pack_id?: string
  internal_design_exported_at?: string
  question_count?: number
  scenario_count?: number
  hypothesis_item_count?: number
  pack_export_readiness?: string
  p3_usage_policy?: string
  p3_context_counts?: Record<string, number>
}

type P4P5Dashboard = {
  import_id: string
  status?: string
  summary: Record<string, number>
  app_counts: Record<string, number>
  themes: P4P5ThemeRow[]
}

function p4p5Href(apiBase: string, path?: string | null) {
  if (!path) return '#'
  if (path.startsWith('http')) return path
  return `${apiBase}${path}`
}


function customerPackDownloadUrl(result: any): string | null {
  if (!result || typeof result !== 'object') return null
  const candidates = [
    result.download_url,
    result.downloadUrl,
    result.links?.download,
    result.links?.download_url,
    result.links?.customer_pack_download,
  ]
  for (const value of candidates) {
    if (typeof value === 'string' && value.trim()) return value
  }
  if (result.import_id && result.pack_id) {
    return `/p4p5/imports/${result.import_id}/customer-packs/${result.pack_id}.zip`
  }
  return null
}

function triggerBrowserDownload(apiBase: string, url: string | null, filename?: string) {
  if (!url) return false
  const href = p4p5Href(apiBase, url)
  const a = document.createElement('a')
  a.href = href
  a.target = '_blank'
  a.rel = 'noreferrer'
  if (filename) a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  return true
}

function P4P5Console() {
  const [apiBase, setApiBase] = useState(apiCandidates[0])
  const [imports, setImports] = useState<P4P5ImportSummary[]>([])
  const [selectedImportId, setSelectedImportId] = useState('')
  const [dashboard, setDashboard] = useState<P4P5Dashboard | null>(null)
  const [selectedApp, setSelectedApp] = useState('all')
  const [selectedThemeKey, setSelectedThemeKey] = useState('')
  const [themeDetail, setThemeDetail] = useState<any>(null)
  const [validation, setValidation] = useState<any>(null)
  const [exportResult, setExportResult] = useState<any>(null)
  const [answerImportResult, setAnswerImportResult] = useState<any>(null)
  const [internalExportResult, setInternalExportResult] = useState<any>(null)
  const [exportThemeKeyInput, setExportThemeKeyInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [logs, setLogs] = useState<LogLine[]>([{ level: 'info', text: 'P4/P5 Theme Catalog Console ready' }])

  const addLog = (level: LogLine['level'], text: string) => setLogs((prev) => [{ level, text }, ...prev].slice(0, 60))

  async function request(path: string, init?: RequestInit) {
    let last: any = null
    for (const base of apiCandidates) {
      try {
        const res = await fetch(`${base}${path}`, init)
        if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
        setApiBase(base)
        return await res.json()
      } catch (e) { last = e }
    }
    throw last
  }

  async function refresh() {
    try {
      const list = await request('/p4p5/imports')
      const items = list.items || []
      setImports(items)
      const nextId = selectedImportId || items[0]?.import_id || ''
      if (nextId) {
        setSelectedImportId(nextId)
        const dash = await request(`/p4p5/imports/${nextId}/dashboard`)
        const val = await request(`/p4p5/imports/${nextId}/validation`)
        setDashboard(dash)
        setValidation(val)
        if (!selectedThemeKey && dash.themes?.[0]) setSelectedThemeKey(dash.themes[0].development_theme_key)
      } else {
        setDashboard(null)
        setValidation(null)
      }
    } catch (e: any) {
      addLog('warn', `P4/P5 import not loaded yet: ${e.message || e}`)
    }
  }

  useEffect(() => { refresh() }, [])
  useEffect(() => {
    if (!selectedImportId) return
    request(`/p4p5/imports/${selectedImportId}/dashboard`).then(setDashboard).catch((e) => addLog('error', e.message || String(e)))
    request(`/p4p5/imports/${selectedImportId}/validation`).then(setValidation).catch(() => {})
  }, [selectedImportId])

  useEffect(() => {
    if (!selectedImportId || !selectedThemeKey) return
    request(`/p4p5/imports/${selectedImportId}/themes/${selectedThemeKey}`).then(setThemeDetail).catch((e) => addLog('error', e.message || String(e)))
  }, [selectedImportId, selectedThemeKey])

  async function uploadCatalog(file: File | null) {
    if (!file) return
    setBusy(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const result = await request('/p4p5/import-theme-catalog', { method: 'POST', body })
      setSelectedImportId(result.import_id)
      addLog('ok', `Imported ${result.theme_count || 0} themes / ${result.total_question_count || 0} questions`)

      // User-facing import flow is intentionally one button:
      // upload/import the whole Catalog ZIP or JSON, then immediately validate it.
      const validationResult = await request(`/p4p5/imports/${result.import_id}/validate`, { method: 'POST' })
      setValidation(validationResult)
      addLog(validationResult.status === 'valid' ? 'ok' : 'warn', `Auto Validate: ${validationResult.status} / errors ${validationResult.error_count || 0} / warnings ${validationResult.warning_count || 0}`)
      await refresh()
    } catch (e: any) {
      addLog('error', `Import failed: ${e.message || e}`)
    } finally { setBusy(false) }
  }

  async function validate() {
    if (!selectedImportId) return
    setBusy(true)
    try {
      const result = await request(`/p4p5/imports/${selectedImportId}/validate`, { method: 'POST' })
      setValidation(result)
      addLog(result.status === 'valid' ? 'ok' : 'warn', `Validate: ${result.status} / errors ${result.error_count || 0} / warnings ${result.warning_count || 0}`)
    } catch (e: any) { addLog('error', e.message || String(e)) } finally { setBusy(false) }
  }

  async function exportPackByKey() {
    if (!selectedImportId) return
    const key = exportThemeKeyInput.trim() || selectedThemeKey
    if (!key) {
      addLog('warn', 'PACK Exportする development_theme_key を入力してください。')
      return
    }
    setBusy(true)
    try {
      const params = new URLSearchParams()
      params.set('theme_key', key)
      params.set('include_p3_diagrams', 'true')
      const result = await request(`/p4p5/imports/${selectedImportId}/customer-pack/export?${params}`, { method: 'POST' })
      setExportResult(result)
      setSelectedThemeKey(key)
      const downloadUrl = customerPackDownloadUrl(result)
      if (triggerBrowserDownload(apiBase, downloadUrl, result?.pack_id ? `${result.pack_id}.zip` : undefined)) {
        addLog('ok', `Exported and opened customer pack download: ${key}`)
      } else {
        addLog('warn', `Exported customer pack, but no download_url was returned: ${key}`)
      }
    } catch (e: any) {
      addLog('error', `PACK Export failed: ${e.message || String(e)}`)
    } finally { setBusy(false) }
  }


  async function importAnsweredPack(file: File | null) {
    if (!file || !selectedImportId) return
    setBusy(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const result = await request(`/p4p5/imports/${selectedImportId}/customer-pack/import`, { method: 'POST', body })
      setAnswerImportResult(result)
      addLog('ok', `Imported answered PACK: ${result.theme_count || 0} theme(s), answered total ${result.answered_theme_count || 0}`)
      const dash = await request(`/p4p5/imports/${selectedImportId}/dashboard`)
      setDashboard(dash)
    } catch (e: any) {
      addLog('error', `Answered PACK Import failed: ${e.message || String(e)}`)
    } finally { setBusy(false) }
  }

  async function exportInternalDesignPack(scope: 'all_answered' | 'selected_theme' = 'all_answered') {
    if (!selectedImportId) return
    setBusy(true)
    try {
      const params = new URLSearchParams()
      if (scope === 'selected_theme' && selectedThemeKey) params.set('theme_key', selectedThemeKey)
      const result = await request(`/p4p5/imports/${selectedImportId}/internal-design-pack/export${params.toString() ? `?${params}` : ''}`, { method: 'POST' })
      setInternalExportResult(result)
      addLog('ok', `Exported Internal Design Pack: ${result.theme_count || 0} theme(s)`)
      const dash = await request(`/p4p5/imports/${selectedImportId}/dashboard`)
      setDashboard(dash)
    } catch (e: any) {
      addLog('error', `Internal Design Pack Export failed: ${e.message || String(e)}`)
    } finally { setBusy(false) }
  }

  async function copyKey(key: string) {
    try {
      await navigator.clipboard.writeText(key)
      setExportThemeKeyInput(key)
      addLog('ok', `Key copied to export input: ${key}`)
    } catch {
      setExportThemeKeyInput(key)
      addLog('ok', `Key set to export input: ${key}`)
    }
  }

  const appKeys = useMemo(() => Object.keys(dashboard?.app_counts || {}), [dashboard])
  const themes = useMemo(() => {
    const rows = dashboard?.themes || []
    return selectedApp === 'all' ? rows : rows.filter((r) => r.app_key === selectedApp || (r.target_apps || []).includes(selectedApp))
  }, [dashboard, selectedApp])
  const selectedTheme = themes.find((t) => t.development_theme_key === selectedThemeKey) || dashboard?.themes?.find((t) => t.development_theme_key === selectedThemeKey)

  const p3Counts = selectedTheme?.p3_context_counts || {}
  const promptSeed = themeDetail?.customer_question_prompt_seed || {}
  const questionBlocks = promptSeed?.question_blocks || []
  const scenarioSeed = promptSeed?.scenario_seed || []
  const odoo = themeDetail?.odoo_mapping_seed || {}
  const p3 = themeDetail?.p3_context_refs || {}

  return (
    <main className="app-shell p4p5-shell">
      <header className="topbar">
        <div>
          <h1>P4/P5 Development Theme Console</h1>
          <p>Import / Validate / 画面表示 / 顧客回答PACK Export。P3情報はreference_onlyとして質問PACKへ差し込みます。</p>
        </div>
        <div className="top-actions">
          <a className="download-link" href="/" title="既存Phase Consoleへ戻る">Phase Console</a>
          <button className="secondary" onClick={refresh} disabled={busy}><RefreshCcw size={16}/> Refresh</button>
        </div>
      </header>

      <section className="layout phase-layout">
        <aside className="panel left-panel">
          <div className="section-title"><Upload size={17}/> One Button Import</div>
          <label className="upload-box small-upload">
            <input type="file" accept=".zip,.json" onChange={(e) => uploadCatalog(e.target.files?.[0] || null)} />
            {busy ? <Loader2 className="spin"/> : <FileArchive/>}
            <span>Import All: Catalog ZIP/JSON + Auto Validate</span>
          </label>

          <div className="section-title spaced">Imports</div>
          <div className="history-list">
            {imports.length ? imports.map((x) => (
              <button key={x.import_id} className={`history-row ${selectedImportId === x.import_id ? 'selected' : ''}`} onClick={() => setSelectedImportId(x.import_id)}>
                <span><b>{x.import_id}</b><small>{fmt(x.theme_count)} themes / {fmt(x.total_question_count)} q</small></span>
                <span className={`pill ${x.status === 'valid' ? 'good' : 'bad'}`}>{x.status || '-'}</span>
              </button>
            )) : <p className="empty">まだP4/P5 CatalogがImportされていません。</p>}
          </div>

          <div className="section-title spaced">Apps</div>
          <div className="app-grid p4p5-app-grid">
            <button className={`app-card ${selectedApp === 'all' ? 'selected' : ''}`} onClick={() => setSelectedApp('all')}><strong>All</strong><span>{fmt(dashboard?.summary?.theme_count)}</span></button>
            {appKeys.map((app) => <button key={app} className={`app-card ${selectedApp === app ? 'selected' : ''}`} onClick={() => setSelectedApp(app)}><strong>{app}</strong><span>{fmt(dashboard?.app_counts?.[app])} themes</span></button>)}
          </div>
        </aside>

        <section className="panel center-panel">
          <div className="section-title"><PackageOpen size={17}/> Theme Catalog</div>
          {dashboard ? <div className="summary-strip p4p5-summary">
            <Metric label="Themes" value={dashboard.summary.theme_count || 0}/>
            <Metric label="Questions" value={dashboard.summary.total_question_count || 0}/>
            <Metric label="P3 Context" value={dashboard.summary.p3_context_theme_count || 0}/>
            <Metric label="Pack Ready" value={dashboard.summary.customer_pack_ready_theme_count || 0}/>
            <Metric label="Answered" value={dashboard.summary.answered_theme_count || 0}/>
            <Metric label="Internal Exported" value={dashboard.summary.internal_exported_theme_count || 0}/>
          </div> : <p className="empty">CatalogをUploadしてください。</p>}

          <div className="theme-table">
            {themes.map((t) => {
              const answered = t.customer_answer_status === 'answered' || t.customer_answer_status === 'answered_with_definition'
              return <button key={t.development_theme_key} className={`theme-row ${answered ? 'answered' : 'unanswered'} ${selectedThemeKey === t.development_theme_key ? 'selected' : ''}`} onClick={() => setSelectedThemeKey(t.development_theme_key)}>
                <div>
                  <strong>{t.title_ja}</strong>
                  <span className="theme-key-line">{t.development_theme_key}</span>
                  <small>{t.business_domain || '-'} / {t.process_stage || '-'}</small>
                </div>
                <div className="theme-badges">
                  <button type="button" className="key-copy-button" onClick={(e) => { e.stopPropagation(); copyKey(t.development_theme_key) }} title="このキーをPACK Export欄へ入れる"><ClipboardCopy size={13}/> Key</button>
                  <span className="pill neutral">{t.app_key}</span>
                  <span className="pill">Q {fmt(t.question_count)}</span>
                  <span className="pill warn">P3 {fmt(Object.values(t.p3_context_counts || {}).reduce((a: any, b: any) => Number(a) + Number(b), 0))}</span>
                  <span className={`pill ${t.customer_answer_status === 'answered' || t.customer_answer_status === 'answered_with_definition' ? 'good' : 'neutral'}`}>{t.customer_answer_status === 'answered' || t.customer_answer_status === 'answered_with_definition' ? '回答済み' : '未回答'}</span>
                  {t.internal_design_export_status === 'exported' && <span className="pill good">内部Export済み</span>}
                </div>
              </button>
            })}
          </div>
        </section>

        <aside className="panel right-panel phase-action-panel">
          <div className="section-title"><CheckCircle2 size={17}/> Validate / Key Export</div>
          <div className={`status-banner ${validation?.status === 'valid' ? 'good-bg' : 'bad-bg'}`}>{validation?.status === 'valid' ? <CheckCircle2 size={18}/> : <AlertTriangle size={18}/>} {validation?.status || 'not imported'}</div>
          <p className="action-hint">Importは左の1ボタンでCatalog全体を取り込み、自動Validateします。PACK Exportは一覧のキーを確認し、下のキー入力から1Themeずつ出力します。</p>
          <button className="secondary" onClick={validate} disabled={!selectedImportId || busy}><CheckCircle2 size={16}/> Re-Validate Catalog</button>
          <div className="key-export-box">
            <label>development_theme_key</label>
            <textarea
              value={exportThemeKeyInput}
              onChange={(e) => setExportThemeKeyInput(e.target.value)}
              placeholder="例: fg_p4p5.sales.customer_requirement_shipping_control.001"
              rows={3}
            />
            <button className="primary" onClick={exportPackByKey} disabled={!selectedImportId || busy}><Download size={15}/> PACK Export by Key（P3図表つき）</button>
          </div>
          {selectedThemeKey && <button className="secondary" onClick={() => setExportThemeKeyInput(selectedThemeKey)} disabled={busy}><ClipboardCopy size={15}/> 選択中Themeキーを入力欄へ</button>}
          {exportResult?.download_url && <a className="download-link" href={p4p5Href(apiBase, exportResult.download_url)} target="_blank" rel="noreferrer">Download Latest Customer PACK ZIP</a>}

          <div className="section-title spaced"><Upload size={17}/> Answered PACK Import</div>
          <label className="upload-box small-upload">
            <input type="file" accept=".zip,.json" onChange={(e) => importAnsweredPack(e.target.files?.[0] || null)} />
            {busy ? <Loader2 className="spin"/> : <FileArchive/>}
            <span>Import Answered PACK ZIP/JSON</span>
          </label>
          {answerImportResult && <p className="action-hint">回答済みImport: {answerImportResult.theme_count || 0} theme / total answered {answerImportResult.answered_theme_count || 0}</p>}

          <div className="section-title spaced"><Download size={17}/> Internal Design Export</div>
          <p className="action-hint">回答済みThemeをChatGPT内部実行用PACKとしてExportします。ここでは反映は行わず、次工程の材料ZIPだけを作成します。</p>
          <button className="primary" onClick={() => exportInternalDesignPack('all_answered')} disabled={!selectedImportId || busy}><Download size={15}/> Export All Answered Themes</button>
          <button className="secondary" onClick={() => exportInternalDesignPack('selected_theme')} disabled={!selectedImportId || !selectedThemeKey || busy}><Download size={15}/> Export Selected Theme</button>
          {internalExportResult?.download_url && <a className="download-link" href={p4p5Href(apiBase, internalExportResult.download_url)} target="_blank" rel="noreferrer">Download Internal Design Pack ZIP</a>}

          {selectedTheme && <div className="detail-card spaced">
            <h3>{selectedTheme.title_ja}</h3>
            <p><b>Key:</b> {selectedTheme.development_theme_key}</p>
            <p><b>Pattern:</b> {selectedTheme.implementation_pattern || '-'}</p>
            <p><b>Risk:</b> {selectedTheme.risk_level || '-'}</p>
            <p><b>Q Blocks:</b> {fmt(selectedTheme.question_count)}</p>
        <p><b>Scenarios:</b> {fmt(selectedTheme.scenario_count)}</p>
        <p><b>Hypotheses:</b> {fmt(selectedTheme.hypothesis_item_count)}</p>
        <p><b>P3 Policy:</b> {selectedTheme.p3_usage_policy || '-'}</p>
            <p><b>回答状態:</b> {selectedTheme.customer_answer_status === 'answered' || selectedTheme.customer_answer_status === 'answered_with_definition' ? '回答済み' : '未回答'}</p>
            <p><b>回答数:</b> {fmt(selectedTheme.answered_count)} / {fmt(selectedTheme.answer_total_count)}</p>
            {selectedTheme.customization_title_ja && <p><b>定義:</b> {selectedTheme.customization_title_ja}</p>}
          </div>}
        </aside>
      </section>

      <section className="layout p4p5-detail-layout">
        <section className="panel">
          <div className="section-title"><Wrench size={17}/> Odoo Mapping Seed</div>
          <MiniList title="標準モデル" items={odoo.standard_models} mainKey="model" subKey="usage_ja" />
          <MiniList title="カスタムモデル候補" items={odoo.custom_model_candidates} mainKey="model" subKey="role_ja" />
          <MiniList title="ロジック候補" items={odoo.custom_logic_candidates} mainKey="logic_key" subKey="title_ja" />
        </section>
        <section className="panel">
          <div className="section-title"><Network size={17}/> P3 Context Reference</div>
          <div className="mini-metrics">
            <Metric label="Support Masters" value={p3Counts.support_masters || 0}/>
            <Metric label="Overlay Fields" value={p3Counts.overlay_fields || 0}/>
            <Metric label="GAP Items" value={p3Counts.gap_items || 0} tone="warn"/>
            <Metric label="Skipped" value={p3Counts.skipped_items || 0} tone="warn"/>
          </div>
          <MiniList title="関連P3補助マスタ" items={p3.related_support_masters || p3.related_p3_support_master_keys} mainKey="label_ja" subKey="key" />
          <MiniList title="関連P3 GAP" items={p3.related_p3_gap_items || p3.related_p3_gap_keys} mainKey="label_ja" subKey="key" />
        </section>
        <section className="panel">
          <div className="section-title"><PackageOpen size={17}/> Customer Questions</div>
          <div className="question-list">
            {scenarioSeed.length > 0 && <div className="question-group"><h3>業務シナリオ</h3>{scenarioSeed.slice(0, 3).map((sc: any) => <div key={sc.scenario_key || sc.title_ja} className="question-card"><b>{sc.title_ja || sc.scenario_key}</b><small>{sc.body_ja}</small></div>)}</div>}
            {questionBlocks.map((g: any) => <div key={g.question_id || g.question_group || g.group_key || g.title_ja} className="question-group"><h3>{g.question_id || g.question_group || g.group_key}. {g.title_ja || g.group_title_ja}</h3><p className="muted-text">{g.question_background_ja || g.decision_purpose_ja}</p>{(g.hypothesis_items || []).map((q: any) => <div key={q.decision_key || q.question_id} className="question-card"><b>{q.label_ja || q.question_id}</b><small>{q.hypothesis_ja}</small><code>{q.decision_key}</code></div>)}</div>)}
          </div>
        </section>
      </section>

      <section className="panel log-panel"><div className="section-title">Run Log</div><pre>{logs.map((l) => `${l.level === 'ok' ? '◎' : l.level === 'warn' ? '⚠' : l.level === 'error' ? '✕' : '○'} ${l.text}`).join('\n')}</pre></section>
    </main>
  )
}

function P4P5EmbeddedConsole() {
  const [apiBase, setApiBase] = useState('/api')
  const [imports, setImports] = useState<P4P5ImportSummary[]>([])
  const [selectedImportId, setSelectedImportId] = useState('')
  const [dashboard, setDashboard] = useState<P4P5Dashboard | null>(null)
  const [selectedApp, setSelectedApp] = useState('all')
  const [selectedThemeKey, setSelectedThemeKey] = useState('')
  const [themeDetail, setThemeDetail] = useState<any>(null)
  const [validation, setValidation] = useState<any>(null)
  const [exportResult, setExportResult] = useState<any>(null)
  const [answerImportResult, setAnswerImportResult] = useState<any>(null)
  const [internalExportResult, setInternalExportResult] = useState<any>(null)
  const [exportThemeKeyInput, setExportThemeKeyInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [logs, setLogs] = useState<LogLine[]>([{ level: 'info', text: 'P4/P5 unified Theme Catalog workspace ready' }])

  function addLog(level: LogLine['level'], text: string) { setLogs((prev) => [{ level, text }, ...prev].slice(0, 30)) }
  async function request(path: string, init?: RequestInit) {
    const { res, base } = await tryFetch(path, init)
    setApiBase(base)
    const data = await readJsonSafe(res)
    if (!res.ok) throw new Error(errorMessage(data, `Request failed: ${path}`))
    return data
  }
  async function refresh() {
    try {
      const list = await request('/p4p5/imports')
      const items = list.items || list.imports || []
      setImports(items)
      const nextId = selectedImportId || items[0]?.import_id || ''
      if (nextId) {
        setSelectedImportId(nextId)
        const dash = await request(`/p4p5/imports/${nextId}/dashboard`)
        const val = await request(`/p4p5/imports/${nextId}/validation`)
        setDashboard(dash)
        setValidation(val)
        if (!selectedThemeKey && dash.themes?.[0]) setSelectedThemeKey(dash.themes[0].development_theme_key)
      } else {
        setDashboard(null)
        setValidation(null)
      }
    } catch (e: any) {
      addLog('warn', `P4/P5 Catalog未Importまたは読込失敗: ${e.message || e}`)
    }
  }
  useEffect(() => { refresh() }, [])
  useEffect(() => {
    if (!selectedImportId) return
    request(`/p4p5/imports/${selectedImportId}/dashboard`).then((dash) => {
      setDashboard(dash)
      if (!selectedThemeKey && dash.themes?.[0]) setSelectedThemeKey(dash.themes[0].development_theme_key)
    }).catch((e) => addLog('error', e.message || String(e)))
    request(`/p4p5/imports/${selectedImportId}/validation`).then(setValidation).catch(() => {})
  }, [selectedImportId])
  useEffect(() => {
    if (!selectedImportId || !selectedThemeKey) return
    request(`/p4p5/imports/${selectedImportId}/themes/${selectedThemeKey}`).then(setThemeDetail).catch((e) => addLog('error', e.message || String(e)))
  }, [selectedImportId, selectedThemeKey])

  async function uploadCatalog(file: File | null) {
    if (!file) return
    setBusy(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const result = await request('/p4p5/import-theme-catalog', { method: 'POST', body })
      setSelectedImportId(result.import_id)
      addLog('ok', `Imported ${result.theme_count || 0} themes / ${result.total_question_count || 0} questions`)
      const validationResult = await request(`/p4p5/imports/${result.import_id}/validate`, { method: 'POST' })
      setValidation(validationResult)
      addLog(validationResult.status === 'valid' ? 'ok' : 'warn', `Auto Validate: ${validationResult.status} / errors ${validationResult.error_count || 0} / warnings ${validationResult.warning_count || 0}`)
      await refresh()
    } catch (e: any) {
      addLog('error', `Import failed: ${e.message || e}`)
    } finally { setBusy(false) }
  }
  async function validate() {
    if (!selectedImportId) return
    setBusy(true)
    try {
      const result = await request(`/p4p5/imports/${selectedImportId}/validate`, { method: 'POST' })
      setValidation(result)
      addLog(result.status === 'valid' ? 'ok' : 'warn', `Validate: ${result.status} / errors ${result.error_count || 0} / warnings ${result.warning_count || 0}`)
    } catch (e: any) { addLog('error', e.message || String(e)) } finally { setBusy(false) }
  }
  async function exportPackByKey() {
    if (!selectedImportId) return
    const key = exportThemeKeyInput.trim() || selectedThemeKey
    if (!key) {
      addLog('warn', 'PACK Exportする development_theme_key を入力してください。')
      return
    }
    setBusy(true)
    try {
      const params = new URLSearchParams()
      params.set('theme_key', key)
      params.set('include_p3_diagrams', 'true')
      const result = await request(`/p4p5/imports/${selectedImportId}/customer-pack/export?${params}`, { method: 'POST' })
      setExportResult(result)
      setSelectedThemeKey(key)
      const downloadUrl = customerPackDownloadUrl(result)
      if (triggerBrowserDownload(apiBase, downloadUrl, result?.pack_id ? `${result.pack_id}.zip` : undefined)) {
        addLog('ok', `Exported and opened customer pack download: ${key}`)
      } else {
        addLog('warn', `Exported customer pack, but no download_url was returned: ${key}`)
      }
    } catch (e: any) {
      addLog('error', `PACK Export failed: ${e.message || String(e)}`)
    } finally { setBusy(false) }
  }

  async function importAnsweredPack(file: File | null) {
    if (!file || !selectedImportId) return
    setBusy(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const result = await request(`/p4p5/imports/${selectedImportId}/customer-pack/import`, { method: 'POST', body })
      setAnswerImportResult(result)
      addLog('ok', `Imported answered PACK: ${result.theme_count || 0} theme(s), answered total ${result.answered_theme_count || 0}`)
      const dash = await request(`/p4p5/imports/${selectedImportId}/dashboard`)
      setDashboard(dash)
    } catch (e: any) {
      addLog('error', `Answered PACK Import failed: ${e.message || String(e)}`)
    } finally { setBusy(false) }
  }

  async function exportInternalDesignPack(scope: 'all_answered' | 'selected_theme' = 'all_answered') {
    if (!selectedImportId) return
    setBusy(true)
    try {
      const params = new URLSearchParams()
      if (scope === 'selected_theme' && selectedThemeKey) params.set('theme_key', selectedThemeKey)
      const result = await request(`/p4p5/imports/${selectedImportId}/internal-design-pack/export${params.toString() ? `?${params}` : ''}`, { method: 'POST' })
      setInternalExportResult(result)
      addLog('ok', `Exported Internal Design Pack: ${result.theme_count || 0} theme(s)`)
      const dash = await request(`/p4p5/imports/${selectedImportId}/dashboard`)
      setDashboard(dash)
    } catch (e: any) {
      addLog('error', `Internal Design Pack Export failed: ${e.message || String(e)}`)
    } finally { setBusy(false) }
  }

  async function copyKey(key: string) {
    try {
      await navigator.clipboard.writeText(key)
      setExportThemeKeyInput(key)
      addLog('ok', `Key copied to export input: ${key}`)
    } catch {
      setExportThemeKeyInput(key)
      addLog('ok', `Key set to export input: ${key}`)
    }
  }

  const appKeys = useMemo(() => Object.keys(dashboard?.app_counts || {}), [dashboard])
  const themes = useMemo(() => {
    const rows = dashboard?.themes || []
    return selectedApp === 'all' ? rows : rows.filter((r) => r.app_key === selectedApp || (r.target_apps || []).includes(selectedApp))
  }, [dashboard, selectedApp])
  const selectedTheme = themes.find((t) => t.development_theme_key === selectedThemeKey) || dashboard?.themes?.find((t) => t.development_theme_key === selectedThemeKey)
  const p3Counts = selectedTheme?.p3_context_counts || {}
  const promptSeed = themeDetail?.customer_question_prompt_seed || {}
  const questionBlocks = promptSeed?.question_blocks || []
  const scenarioSeed = promptSeed?.scenario_seed || []
  const odoo = themeDetail?.odoo_mapping_seed || {}
  const p3 = themeDetail?.p3_context_refs || {}

  return <>
    <section className="panel center-panel p4p5-embedded-center">
      <div className="section-title"><PackageOpen size={17}/> P4/P5 Unified Theme Catalog</div>
      <p className="info-note">左パネルのP4またはP5を選択すると、この統合Theme一覧を表示します。P4/P5は分けず、Development Theme単位で顧客回答PACKをExportします。</p>
      <label className="upload-box small-upload p4p5-import-box">
        <input type="file" accept=".zip,.json" onChange={(e) => uploadCatalog(e.target.files?.[0] || null)} />
        {busy ? <Loader2 className="spin"/> : <FileArchive/>}
        <span>Import All: Catalog ZIP/JSON + Auto Validate</span>
      </label>
      <div className="embedded-toolbar">
        <button className="secondary" onClick={refresh} disabled={busy}><RefreshCcw size={16}/> Refresh</button>
        <button className="secondary" onClick={validate} disabled={!selectedImportId || busy}><CheckCircle2 size={16}/> Re-Validate</button>
        {imports.length > 0 && <select className="select-input" value={selectedImportId} onChange={(e) => setSelectedImportId(e.target.value)}>
          {imports.map((x) => <option key={x.import_id} value={x.import_id}>{x.import_id} / {x.status || '-'} / {fmt(x.theme_count)} themes</option>)}
        </select>}
      </div>
      {dashboard ? <div className="summary-strip p4p5-summary">
        <Metric label="Themes" value={dashboard.summary.theme_count || 0}/>
        <Metric label="Q Blocks" value={dashboard.summary.total_question_count || 0}/>
        <Metric label="Hypotheses" value={dashboard.summary.total_hypothesis_item_count || 0}/>
        <Metric label="Scenarios" value={dashboard.summary.total_scenario_count || 0}/>
        <Metric label="P3 Context" value={dashboard.summary.p3_context_theme_count || 0}/>
        <Metric label="Pack Ready" value={dashboard.summary.customer_pack_ready_theme_count || 0}/>
        <Metric label="Answered" value={dashboard.summary.answered_theme_count || 0}/>
        <Metric label="Internal Exported" value={dashboard.summary.internal_exported_theme_count || 0}/>
      </div> : <p className="empty">P4/P5 CatalogをImportしてください。</p>}
      <div className="app-grid p4p5-app-grid compact-app-grid">
        <button className={`app-card ${selectedApp === 'all' ? 'selected' : ''}`} onClick={() => setSelectedApp('all')}><strong>All</strong><span>{fmt(dashboard?.summary?.theme_count)} themes</span></button>
        {appKeys.map((app) => <button key={app} className={`app-card ${selectedApp === app ? 'selected' : ''}`} onClick={() => setSelectedApp(app)}><strong>{app}</strong><span>{fmt(dashboard?.app_counts?.[app])} themes</span></button>)}
      </div>
      <div className="theme-table embedded-theme-table">
        {themes.map((t) => {
          const answered = t.customer_answer_status === 'answered' || t.customer_answer_status === 'answered_with_definition'
          return <button key={t.development_theme_key} className={`theme-row ${answered ? 'answered' : 'unanswered'} ${selectedThemeKey === t.development_theme_key ? 'selected' : ''}`} onClick={() => setSelectedThemeKey(t.development_theme_key)}>
            <div>
              <strong>{t.title_ja}</strong>
              <span className="theme-key-line">{t.development_theme_key}</span>
              <small>{t.business_domain || '-'} / {t.process_stage || '-'}</small>
            </div>
            <div className="theme-badges">
              <button type="button" className="key-copy-button" onClick={(e) => { e.stopPropagation(); copyKey(t.development_theme_key) }} title="このキーをPACK Export欄へ入れる"><ClipboardCopy size={13}/> Key</button>
              <span className="pill neutral">{t.app_key}</span>
              <span className="pill">Q {fmt(t.question_count)}</span>
              <span className="pill neutral">S {fmt(t.scenario_count)}</span>
              <span className="pill neutral">H {fmt(t.hypothesis_item_count)}</span>
              <span className="pill warn">P3 {fmt(Object.values(t.p3_context_counts || {}).reduce((a: any, b: any) => Number(a) + Number(b), 0))}</span>
              <span className={`pill ${t.customer_answer_status === 'answered' || t.customer_answer_status === 'answered_with_definition' ? 'good' : 'neutral'}`}>{t.customer_answer_status === 'answered' || t.customer_answer_status === 'answered_with_definition' ? '回答済み' : '未回答'}</span>
              {t.internal_design_export_status === 'exported' && <span className="pill good">内部Export済み</span>}
            </div>
          </button>
        })}
      </div>
      <div className="p4p5-lower-grid">
        <div className="detail-card">
          <h3>Odoo Mapping Seed</h3>
          <MiniList title="標準モデル" items={odoo.standard_models} mainKey="model" subKey="usage_ja" />
          <MiniList title="カスタムモデル候補" items={odoo.custom_model_candidates} mainKey="model" subKey="role_ja" />
          <MiniList title="ロジック候補" items={odoo.custom_logic_candidates} mainKey="logic_key" subKey="title_ja" />
        </div>
        <div className="detail-card">
          <h3>P3 Context Reference</h3>
          <div className="mini-metrics">
            <Metric label="Support Masters" value={p3Counts.support_masters || 0}/>
            <Metric label="Overlay Fields" value={p3Counts.overlay_fields || 0}/>
            <Metric label="GAP Items" value={p3Counts.gap_items || 0} tone="warn"/>
            <Metric label="Skipped" value={p3Counts.skipped_items || 0} tone="warn"/>
          </div>
          <MiniList title="関連P3補助マスタ" items={p3.related_support_masters || p3.related_p3_support_master_keys} mainKey="label_ja" subKey="key" />
          <MiniList title="関連P3 GAP" items={p3.related_p3_gap_items || p3.related_p3_gap_keys} mainKey="label_ja" subKey="key" />
        </div>
      </div>
    </section>
    <aside className="panel right-panel phase-action-panel p4p5-embedded-right">
      <div className="section-title"><CheckCircle2 size={17}/> Validate / Key Export</div>
      <div className={`status-banner ${validation?.status === 'valid' ? 'good-bg' : 'bad-bg'}`}>{validation?.status === 'valid' ? <CheckCircle2 size={18}/> : <AlertTriangle size={18}/>} {validation?.status || 'not imported'}</div>
      <p className="action-hint">一覧でdevelopment_theme_keyを確認し、Keyボタンで入力欄へ入れて、1Theme単位で顧客回答PACKをExportします。</p>
      <div className="key-export-box">
        <label>development_theme_key</label>
        <textarea
          value={exportThemeKeyInput}
          onChange={(e) => setExportThemeKeyInput(e.target.value)}
          placeholder="例: fg_p4p5.sales.customer_requirement_shipping_control.001"
          rows={3}
        />
        <button className="primary" onClick={exportPackByKey} disabled={!selectedImportId || busy}><Download size={15}/> PACK Export by Key（P3図表つき）</button>
      </div>
      {selectedThemeKey && <button className="secondary" onClick={() => setExportThemeKeyInput(selectedThemeKey)} disabled={busy}><ClipboardCopy size={15}/> 選択中Themeキーを入力欄へ</button>}
      {exportResult?.download_url && <a className="download-link" href={p4p5Href(apiBase, exportResult.download_url)} target="_blank" rel="noreferrer">Download Latest Customer PACK ZIP</a>}

      <div className="section-title spaced"><Upload size={17}/> Answered PACK Import</div>
      <label className="upload-box small-upload">
        <input type="file" accept=".zip,.json" onChange={(e) => importAnsweredPack(e.target.files?.[0] || null)} />
        {busy ? <Loader2 className="spin"/> : <FileArchive/>}
        <span>Import Answered PACK ZIP/JSON</span>
      </label>
      {answerImportResult && <p className="action-hint">回答済みImport: {answerImportResult.theme_count || 0} theme / total answered {answerImportResult.answered_theme_count || 0}</p>}

      <div className="section-title spaced"><Download size={17}/> Internal Design Export</div>
      <p className="action-hint">回答済みThemeをChatGPT内部実行用PACKとしてExportします。ここでは反映は行わず、次工程の材料ZIPだけを作成します。</p>
      <button className="primary" onClick={() => exportInternalDesignPack('all_answered')} disabled={!selectedImportId || busy}><Download size={15}/> Export All Answered Themes</button>
      <button className="secondary" onClick={() => exportInternalDesignPack('selected_theme')} disabled={!selectedImportId || !selectedThemeKey || busy}><Download size={15}/> Export Selected Theme</button>
      {internalExportResult?.download_url && <a className="download-link" href={p4p5Href(apiBase, internalExportResult.download_url)} target="_blank" rel="noreferrer">Download Internal Design Pack ZIP</a>}

      {selectedTheme && <div className="detail-card spaced">
        <h3>{selectedTheme.title_ja}</h3>
        <p><b>Key:</b> {selectedTheme.development_theme_key}</p>
        <p><b>Pattern:</b> {selectedTheme.implementation_pattern || '-'}</p>
        <p><b>Risk:</b> {selectedTheme.risk_level || '-'}</p>
        <p><b>Q Blocks:</b> {fmt(selectedTheme.question_count)}</p>
        <p><b>Scenarios:</b> {fmt(selectedTheme.scenario_count)}</p>
        <p><b>Hypotheses:</b> {fmt(selectedTheme.hypothesis_item_count)}</p>
        <p><b>P3 Policy:</b> {selectedTheme.p3_usage_policy || '-'}</p>
        <p><b>回答状態:</b> {selectedTheme.customer_answer_status === 'answered' || selectedTheme.customer_answer_status === 'answered_with_definition' ? '回答済み' : '未回答'}</p>
        <p><b>回答数:</b> {fmt(selectedTheme.answered_count)} / {fmt(selectedTheme.answer_total_count)}</p>
        {selectedTheme.customization_title_ja && <p><b>定義:</b> {selectedTheme.customization_title_ja}</p>}
      </div>}
      <div className="section-title spaced"><PackageOpen size={17}/> Customer Questions</div>
      <div className="question-list compact-questions">
        {scenarioSeed.length > 0 && <div className="question-group"><h3>業務シナリオ</h3>{scenarioSeed.slice(0, 3).map((sc: any) => <div key={sc.scenario_key || sc.title_ja} className="question-card"><b>{sc.title_ja || sc.scenario_key}</b><small>{sc.body_ja}</small></div>)}</div>}
        {questionBlocks.map((g: any) => <div key={g.question_id || g.question_group || g.group_key || g.title_ja} className="question-group"><h3>{g.question_id || g.question_group || g.group_key}. {g.title_ja || g.group_title_ja}</h3><p className="muted-text">{g.question_background_ja || g.decision_purpose_ja}</p>{(g.hypothesis_items || []).slice(0, 6).map((q: any) => <div key={q.decision_key || q.question_id} className="question-card"><b>{q.label_ja || q.question_id}</b><small>{q.hypothesis_ja}</small><code>{q.decision_key}</code></div>)}</div>)}
      </div>
      <div className="section-title spaced">P4/P5 Log</div>
      <pre className="mini-log">{logs.map((l) => `${l.level === 'ok' ? '◎' : l.level === 'warn' ? '⚠' : l.level === 'error' ? '✕' : '○'} ${l.text}`).join('\n')}</pre>
    </aside>
  </>
}


function AuthorityGraphViewer({ payload, selected, onSelect }: { payload: any | null; selected: any | null; onSelect: (item: any) => void }) {
  const nodes = Array.isArray(payload?.nodes) ? payload.nodes : []
  const edges = Array.isArray(payload?.edges) ? payload.edges : []
  const width = 1180
  const height = Math.max(520, Math.ceil(nodes.length / 8) * 110)
  const groups = Array.from(new Set(nodes.map((n: any) => n.group || n.type || 'other')))
  const groupIndex = new Map(groups.map((g, i) => [g, i]))
  const positioned = nodes.map((node: any, idx: number) => {
    const gi = groupIndex.get(node.group || node.type || 'other') || 0
    const colCount = Math.max(1, groups.length)
    const colWidth = width / colCount
    const withinGroupIndex = nodes.filter((n: any, j: number) => j < idx && (n.group || n.type || 'other') === (node.group || node.type || 'other')).length
    const x = Math.min(width - 170, Math.max(40, gi * colWidth + 30))
    const y = 70 + withinGroupIndex * 82
    return { ...node, x, y }
  })
  const byId = new Map(positioned.map((n: any) => [n.id, n]))
  if (!payload) return <div className="authority-graph-empty">Viewを選択して読み込むと、yFiles payloadをプレビューします。</div>
  return <div className="authority-graph-wrap">
    <div className="authority-graph-head">
      <div><b>{payload.title_ja || payload.view_key || 'Authority View'}</b><span>{payload.view_type || '-'}</span></div>
      <div className="mini-metrics"><Metric label="Nodes" value={nodes.length}/><Metric label="Edges" value={edges.length}/></div>
    </div>
    <div className="authority-graph-canvas">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={payload.title_ja || 'Authority graph'}>
        <defs>
          <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" /></marker>
        </defs>
        {groups.map((g, i) => <text key={String(g)} x={i * (width / Math.max(groups.length, 1)) + 30} y={30} className="graph-group-label">{String(g)}</text>)}
        {edges.slice(0, 900).map((edge: any, idx: number) => {
          const source: any = byId.get(edge.source)
          const target: any = byId.get(edge.target)
          if (!source || !target) return null
          const sx = source.x + 70, sy = source.y + 22, tx = target.x + 70, ty = target.y + 22
          const midX = (sx + tx) / 2, midY = (sy + ty) / 2
          return <g key={edge.id || idx} onClick={() => onSelect({ ...edge, element_kind: 'edge' })} className={`graph-edge ${selected?.id === edge.id ? 'selected' : ''}`}>
            <line x1={sx} y1={sy} x2={tx} y2={ty} markerEnd="url(#arrow)" />
            {edge.label && <text x={midX} y={midY - 4} className="graph-edge-label">{edge.label}</text>}
          </g>
        })}
        {positioned.slice(0, 900).map((node: any) => <g key={node.id} className={`graph-node ${node.group || node.type || 'other'} ${selected?.id === node.id ? 'selected' : ''}`} transform={`translate(${node.x},${node.y})`} onClick={() => onSelect({ ...node, element_kind: 'node' })}>
          <rect rx="10" ry="10" width="145" height="48" />
          <text x="10" y="20" className="graph-node-label">{String(node.label || node.id).slice(0, 18)}</text>
          <text x="10" y="36" className="graph-node-type">{String(node.type || node.group || '').slice(0, 20)}</text>
        </g>)}
      </svg>
    </div>
    <p className="action-hint">これはyFiles payloadを使った軽量プレビューです。実装上は同じnodes/edgesをyFiles GraphComponentへ渡せる構造になっています。</p>
  </div>
}

function MiniList({ title, items, mainKey, subKey }: { title: string; items?: any[]; mainKey: string; subKey?: string }) {
  const arr = Array.isArray(items) ? items : []
  return <div className="mini-list-block"><h3>{title}</h3>{arr.length ? arr.slice(0, 8).map((item, idx) => {
    const main = typeof item === 'string' ? item : (item?.[mainKey] || item?.model || item?.key || item?.node_key || JSON.stringify(item))
    const sub = typeof item === 'string' ? '' : (subKey ? item?.[subKey] : '')
    return <p key={`${main}-${idx}`}><b>{main}</b>{sub ? <span>{sub}</span> : null}</p>
  }) : <p className="empty">なし</p>}{arr.length > 8 && <small>他 {arr.length - 8} 件</small>}</div>
}

const RootApp = (window.location.pathname.includes('p4p5') || window.location.search.includes('p4p5')) ? P4P5Console : App
createRoot(document.getElementById('root')!).render(<ConsoleErrorBoundary><RootApp /></ConsoleErrorBoundary>)
