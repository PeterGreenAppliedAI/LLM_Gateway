import { useEffect, useMemo, useRef, useState } from 'react'
import type { MediaEndpoint, MediaVoice, ParamSpec } from '../types'
import { fetchMediaCatalog, synthesizeSpeech, transcribeAudio } from '../lib/api'
import { formatTimestamp } from '../lib/format'

// Formats a browser <audio> element can play (pcm is headerless raw samples)
const PLAYABLE = ['mp3', 'wav', 'opus', 'flac', 'aac']
const STT_FORMATS = ['json', 'text', 'verbose_json', 'srt', 'vtt']

const inputClass = 'bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm w-full'
const labelClass = 'block text-xs text-gray-400 mb-1'
const buttonClass = 'px-3 py-1.5 rounded text-sm bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500'

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3 text-left">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">{title}</h2>
        {right}
      </div>
      {children}
    </div>
  )
}

/** Endpoint dropdown: "Auto" lets the gateway route; a name pins via endpoint/model. */
function EndpointSelect({ endpoints, value, onChange }: { endpoints: MediaEndpoint[]; value: string; onChange: (v: string) => void }) {
  return (
    <select className={inputClass} value={value} onChange={e => onChange(e.target.value)}>
      <option value="">Auto (gateway routes)</option>
      {endpoints.map(ep => (
        <option key={ep.endpoint} value={ep.endpoint}>{ep.endpoint}{ep.healthy ? '' : ' (unhealthy)'}</option>
      ))}
    </select>
  )
}

function modelIds(endpoints: MediaEndpoint[], task?: string): string[] {
  const ids = endpoints.flatMap(ep => ep.models.filter(m => !task || !m.task || m.task === task).map(m => m.id))
  return [...new Set(ids)].sort()
}

// =============================================================================
// Engines
// =============================================================================

function Engines({ endpoints, onRefresh, refreshing }: { endpoints: MediaEndpoint[]; onRefresh: () => void; refreshing: boolean }) {
  return (
    <Card
      title="Voice Engines"
      right={<button className={buttonClass} onClick={onRefresh} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>}
    >
      {endpoints.length === 0 ? (
        <div className="text-sm text-gray-400">
          No voice endpoints configured. Add one in <code>gateway.yaml</code> with <code>type: openai</code> and{' '}
          <code>capabilities: [tts]</code> or <code>[stt]</code>. See{' '}
          <a className="text-blue-400 hover:text-blue-300 underline" target="_blank" rel="noreferrer"
            href="https://github.com/PeterGreenAppliedAI/LLM_Gateway/blob/main/docs/MEDIA_ENGINES.md">
            docs/MEDIA_ENGINES.md
          </a>.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {endpoints.map(ep => (
            <div key={ep.endpoint} className={`bg-gray-900 rounded p-3 border ${ep.healthy ? 'border-green-700' : 'border-red-700'}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold">{ep.endpoint}</span>
                <span className="flex gap-1">
                  {ep.capabilities.map(c => (
                    <span key={c} className="px-2 py-0.5 rounded text-xs bg-blue-900 text-blue-300 uppercase">{c}</span>
                  ))}
                  <span className={`px-2 py-0.5 rounded text-xs ${ep.healthy ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                    {ep.healthy ? 'healthy' : 'unhealthy'}
                  </span>
                </span>
              </div>
              <div className="text-xs text-gray-400 mt-1">
                {ep.profile ? <>Profile <code>{ep.profile}</code>{ep.description ? ` · ${ep.description}` : ''}</> : 'No profile (generic controls)'}
              </div>
              <div className="text-xs text-gray-400 mt-1">
                {ep.voices.length > 0 && <>{ep.voices.length} voices ({ep.voices_source === 'config' ? 'declared in config' : 'from engine'}) · </>}
                {ep.models.length} model{ep.models.length === 1 ? '' : 's'}
                {ep.fetched_at && <> · refreshed {formatTimestamp(ep.fetched_at)}</>}
              </div>
              {ep.error && <div className="text-xs text-orange-300 mt-1">Discovery: {ep.error}</div>}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

// =============================================================================
// Text-to-speech
// =============================================================================

interface BlendPart { voice: string; weight: number }

function blendString(parts: BlendPart[]): string {
  return parts
    .filter(p => p.voice)
    .map(p => (p.weight === 1 ? p.voice : `${p.voice}(${Number(p.weight.toFixed(2))})`))
    .join('+')
}

function SpeechPlayground({ endpoints }: { endpoints: MediaEndpoint[] }) {
  const [endpoint, setEndpoint] = useState('')
  const scoped = endpoint ? endpoints.filter(e => e.endpoint === endpoint) : endpoints
  const voices: MediaVoice[] = useMemo(() => {
    const byId = new Map<string, MediaVoice>()
    scoped.forEach(ep => ep.voices.forEach(v => byId.set(v.id, v)))
    return [...byId.values()].sort((a, b) => a.id.localeCompare(b.id))
  }, [scoped])
  const languages = [...new Set(voices.map(v => v.language).filter(Boolean))].sort() as string[]
  const genders = [...new Set(voices.map(v => v.gender).filter(Boolean))].sort() as string[]
  const params: Record<string, ParamSpec> = scoped.find(e => Object.keys(e.tts_params).length)?.tts_params ?? {}
  const canBlend = scoped.some(e => e.blending)
  const models = modelIds(scoped, 'text-to-speech')

  const [model, setModel] = useState('')
  const [language, setLanguage] = useState('')
  const [gender, setGender] = useState('')
  const [voice, setVoice] = useState('')
  const [blending, setBlending] = useState(false)
  const [blend, setBlend] = useState<BlendPart[]>([{ voice: '', weight: 1 }, { voice: '', weight: 1 }])
  const [speed, setSpeed] = useState(1)
  const [format, setFormat] = useState('mp3')
  const [text, setText] = useState('Hello! This is the gateway speaking.')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ url: string; size: number; ms: number } | null>(null)

  const filtered = voices.filter(v => (!language || v.language === language) && (!gender || v.gender === gender))
  const speedSpec = params.speed
  const formats = (params.response_format?.values as string[] | undefined)?.filter(f => PLAYABLE.includes(f)) ?? PLAYABLE
  const effectiveModel = model || models[0] || ''
  const voiceValue = blending ? blendString(blend) : voice

  useEffect(() => () => { if (result) URL.revokeObjectURL(result.url) }, [result])

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      const body: Record<string, unknown> = {
        model: endpoint ? `${endpoint}/${effectiveModel}` : effectiveModel,
        input: text,
        voice: voiceValue,
        response_format: format,
      }
      if (speed !== 1) body.speed = speed
      const { audio, ms } = await synthesizeSpeech(body)
      setResult({ url: URL.createObjectURL(audio), size: audio.size, ms })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Text-to-Speech">
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div><label className={labelClass}>Endpoint</label><EndpointSelect endpoints={endpoints} value={endpoint} onChange={setEndpoint} /></div>
        <div>
          <label className={labelClass}>Model</label>
          {models.length ? (
            <select className={inputClass} value={effectiveModel} onChange={e => setModel(e.target.value)}>
              {models.map(m => <option key={m}>{m}</option>)}
            </select>
          ) : (
            <input className={inputClass} placeholder="e.g. kokoro" value={model} onChange={e => setModel(e.target.value)} />
          )}
        </div>
        {languages.length > 0 && (
          <div>
            <label className={labelClass}>Language</label>
            <select className={inputClass} value={language} onChange={e => setLanguage(e.target.value)}>
              <option value="">All ({languages.length})</option>
              {languages.map(l => <option key={l}>{l}</option>)}
            </select>
          </div>
        )}
        {genders.length > 0 && (
          <div>
            <label className={labelClass}>Gender</label>
            <select className={inputClass} value={gender} onChange={e => setGender(e.target.value)}>
              <option value="">All</option>
              {genders.map(g => <option key={g}>{g}</option>)}
            </select>
          </div>
        )}
      </div>

      <div>
        <div className="flex items-center gap-3 mb-1">
          <label className="text-xs text-gray-400">Voice</label>
          {canBlend && (
            <label className="flex items-center gap-1 text-xs text-gray-300 cursor-pointer">
              <input type="checkbox" checked={blending} onChange={e => setBlending(e.target.checked)} /> Blend voices
            </label>
          )}
        </div>
        {!blending ? (
          filtered.length ? (
            <select className={inputClass} value={voice} onChange={e => setVoice(e.target.value)}>
              <option value="">Choose a voice ({filtered.length})</option>
              {filtered.map(v => (
                <option key={v.id} value={v.id}>
                  {v.name}{v.language || v.gender ? ` — ${[v.language, v.gender].filter(Boolean).join(', ')}` : ''}
                </option>
              ))}
            </select>
          ) : (
            <input className={inputClass} placeholder="Voice name (this engine doesn't list its voices)" value={voice} onChange={e => setVoice(e.target.value)} />
          )
        ) : (
          <div className="space-y-2">
            {blend.map((part, i) => (
              <div key={i} className="flex items-center gap-2">
                <select className={inputClass} value={part.voice}
                  onChange={e => setBlend(blend.map((p, j) => (j === i ? { ...p, voice: e.target.value } : p)))}>
                  <option value="">Voice {i + 1}</option>
                  {filtered.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
                </select>
                <input type="range" min={0.1} max={3} step={0.1} value={part.weight} className="w-40"
                  onChange={e => setBlend(blend.map((p, j) => (j === i ? { ...p, weight: Number(e.target.value) } : p)))} />
                <span className="text-xs font-mono w-10">{part.weight.toFixed(1)}</span>
                {blend.length > 2 && (
                  <button className="text-xs text-gray-400 hover:text-red-400" onClick={() => setBlend(blend.filter((_, j) => j !== i))}>remove</button>
                )}
              </div>
            ))}
            <div className="flex items-center gap-3 text-xs">
              <button className="text-blue-400 hover:text-blue-300" onClick={() => setBlend([...blend, { voice: '', weight: 1 }])}>+ add voice</button>
              <span className="text-gray-500">Sent as <code>{voiceValue || '—'}</code> (weights are normalized by the engine)</span>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className={labelClass}>Speed: {speed.toFixed(2)}×{speedSpec ? ` (range ${speedSpec.min}–${speedSpec.max})` : ''}</label>
          <input type="range" className="w-full" min={speedSpec?.min ?? 0.25} max={speedSpec?.max ?? 4} step={0.05} value={speed}
            onChange={e => setSpeed(Number(e.target.value))} />
        </div>
        <div>
          <label className={labelClass}>Format</label>
          <select className={inputClass} value={format} onChange={e => setFormat(e.target.value)}>
            {formats.map(f => <option key={f}>{f}</option>)}
          </select>
        </div>
      </div>

      <div>
        <label className={labelClass}>Text ({text.length} characters)</label>
        <textarea className={`${inputClass} h-24`} value={text} onChange={e => setText(e.target.value)} />
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button className={buttonClass} disabled={busy || !text || !voiceValue || !effectiveModel} onClick={generate}>
          {busy ? 'Generating…' : 'Generate speech'}
        </button>
        {error && <span className="text-sm text-red-400">{error}</span>}
      </div>

      {result && (
        <div className="flex items-center gap-3 flex-wrap">
          <audio controls src={result.url} autoPlay />
          <a className="text-sm text-blue-400 hover:text-blue-300" href={result.url} download={`speech.${format}`}>Download</a>
          <span className="text-xs text-gray-500">{(result.size / 1024).toFixed(1)} KB · {(result.ms / 1000).toFixed(2)} s</span>
        </div>
      )}
    </Card>
  )
}

// =============================================================================
// Speech-to-text
// =============================================================================

function TranscribePlayground({ endpoints }: { endpoints: MediaEndpoint[] }) {
  const [endpoint, setEndpoint] = useState('')
  const scoped = endpoint ? endpoints.filter(e => e.endpoint === endpoint) : endpoints
  const models = modelIds(scoped, 'automatic-speech-recognition')
  const languages = [...new Set(scoped.flatMap(e => e.stt_languages))]
  const formats = (scoped.find(e => e.stt_params.response_format)?.stt_params.response_format?.values as string[] | undefined) ?? STT_FORMATS

  const [model, setModel] = useState('')
  const [language, setLanguage] = useState('')
  const [format, setFormat] = useState('json')
  const [task, setTask] = useState<'transcriptions' | 'translations'>('transcriptions')
  const [audio, setAudio] = useState<{ blob: Blob; name: string } | null>(null)
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ text: string; ms: number } | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)

  const effectiveModel = model || models[0] || ''
  const canRecord = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && window.isSecureContext

  const startRecording = async () => {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const rec = new MediaRecorder(stream)
      const chunks: Blob[] = []
      rec.ondataavailable = e => chunks.push(e.data)
      rec.onstop = () => {
        stream.getTracks().forEach(t => t.stop())
        const type = rec.mimeType || 'audio/webm'
        setAudio({ blob: new Blob(chunks, { type }), name: `recording.${type.includes('ogg') ? 'ogg' : 'webm'}` })
        setRecording(false)
      }
      rec.start()
      recorder.current = rec
      setRecording(true)
    } catch (e) {
      setError(`Microphone unavailable: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const submit = async () => {
    if (!audio) return
    setBusy(true)
    setError(null)
    try {
      const fields: Record<string, string> = {
        model: endpoint ? `${endpoint}/${effectiveModel}` : effectiveModel,
        response_format: format,
      }
      if (language && task === 'transcriptions') fields.language = language
      setResult(await transcribeAudio(task, audio.blob, audio.name, fields))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  let shown = result?.text ?? ''
  try { if (result && (format === 'json' || format === 'verbose_json')) shown = JSON.stringify(JSON.parse(result.text), null, 2) } catch { /* show raw */ }

  return (
    <Card title="Speech-to-Text">
      <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
        <div><label className={labelClass}>Endpoint</label><EndpointSelect endpoints={endpoints} value={endpoint} onChange={setEndpoint} /></div>
        <div>
          <label className={labelClass}>Model</label>
          {models.length ? (
            <select className={inputClass} value={effectiveModel} onChange={e => setModel(e.target.value)}>
              {models.map(m => <option key={m}>{m}</option>)}
            </select>
          ) : (
            <input className={inputClass} placeholder="e.g. Systran/faster-whisper-small" value={model} onChange={e => setModel(e.target.value)} />
          )}
        </div>
        <div>
          <label className={labelClass}>Task</label>
          <select className={inputClass} value={task} onChange={e => setTask(e.target.value as typeof task)}>
            <option value="transcriptions">Transcribe</option>
            <option value="translations">Translate to English</option>
          </select>
        </div>
        <div>
          <label className={labelClass}>Language</label>
          <select className={inputClass} value={language} disabled={task === 'translations'} onChange={e => setLanguage(e.target.value)}>
            <option value="">Auto-detect</option>
            {languages.map(l => <option key={l}>{l}</option>)}
          </select>
        </div>
        <div>
          <label className={labelClass}>Output</label>
          <select className={inputClass} value={format} onChange={e => setFormat(e.target.value)}>
            {formats.map(f => <option key={f}>{f}</option>)}
          </select>
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <input type="file" accept="audio/*,video/*" className="text-sm"
          onChange={e => { const f = e.target.files?.[0]; if (f) setAudio({ blob: f, name: f.name }) }} />
        {canRecord ? (
          recording ? (
            <button className="px-3 py-1.5 rounded text-sm bg-red-600 hover:bg-red-500" onClick={() => recorder.current?.stop()}>■ Stop recording</button>
          ) : (
            <button className="px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600" onClick={startRecording}>● Record</button>
          )
        ) : (
          <span className="text-xs text-gray-500">Recording needs HTTPS or localhost</span>
        )}
        {audio && <span className="text-xs text-gray-400">{audio.name} · {(audio.blob.size / 1024).toFixed(1)} KB</span>}
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button className={buttonClass} disabled={busy || !audio || !effectiveModel} onClick={submit}>
          {busy ? 'Transcribing…' : task === 'translations' ? 'Translate' : 'Transcribe'}
        </button>
        {error && <span className="text-sm text-red-400">{error}</span>}
        {result && <span className="text-xs text-gray-500">{(result.ms / 1000).toFixed(2)} s</span>}
      </div>

      {result && <pre className="bg-gray-900 rounded p-3 text-sm whitespace-pre-wrap max-h-80 overflow-auto">{shown}</pre>}
    </Card>
  )
}

// =============================================================================

export function VoiceSection() {
  const [endpoints, setEndpoints] = useState<MediaEndpoint[] | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => { fetchMediaCatalog().then(setEndpoints) }, [])

  const refresh = async () => {
    setRefreshing(true)
    setEndpoints(await fetchMediaCatalog(true))
    setRefreshing(false)
  }

  if (!endpoints) return <div className="text-gray-400">Loading voice engines…</div>
  const tts = endpoints.filter(e => e.capabilities.includes('tts'))
  const stt = endpoints.filter(e => e.capabilities.includes('stt'))

  return (
    <div className="space-y-4">
      <Engines endpoints={endpoints} onRefresh={refresh} refreshing={refreshing} />
      {tts.length > 0 && <SpeechPlayground endpoints={tts} />}
      {stt.length > 0 && <TranscribePlayground endpoints={stt} />}
      <div className="text-xs text-gray-500 text-left">
        Playground requests go through the gateway's public routes with your key: they are audited, metered and count against budgets like any client.
      </div>
    </div>
  )
}
