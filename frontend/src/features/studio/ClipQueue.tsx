import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { t } from '../../i18n'
import { studioApi, type QueueItem, type QueueView } from './api'
import './clip-queue.css'

/** "Today 14:32" / "21 Nov 14:32" in the viewer's own time zone. */
function started(iso: string | null): string {
  if (!iso) return ''
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return at.toDateString() === new Date().toDateString() ? `${t('Today')} ${time}` : `${at.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${time}`
}
const FAST_MS = 4000
const SLOW_MS = 20000
const FINISHED_SHOWN = 6

const STATE_LABEL: Record<string, string> = {
  queued: 'Waiting for a free slot',
  downloading: 'Downloading the part',
  analyzing: 'Analysing the clip',
  rendering: 'Rendering the video',
  done: 'Done',
  failed: 'Failed',
  skipped: 'Skipped (no gameplay)',
}

/** Every recent clip with its live stage and a progress bar; refreshes quickly while anything is queued or running. */
export default function ClipQueue() {
  const navigate = useNavigate()
  const [view, setView] = useState<QueueView | null>(null)
  const [all, setAll] = useState(false)
  const timer = useRef<number>(0)

  useEffect(() => {
    let stopped = false
    const tick = async () => {
      let delay = SLOW_MS
      try {
        const next = await studioApi.queue()
        if (stopped) return
        setView(next)
        if (next.counts.queued + next.counts.running > 0) delay = FAST_MS
      } catch { /* the next tick tries again */ }
      if (!stopped) timer.current = window.setTimeout(() => void tick(), delay)
    }
    void tick()
    return () => { stopped = true; window.clearTimeout(timer.current) }
  }, [])

  if (!view || view.items.length === 0) return null
  const active = view.items.filter(item => item.kind === 'queued' || item.kind === 'running')
  if (active.length === 0 && !all && view.items.every(item => item.kind === 'done')) return null
  const finished = view.items.filter(item => item.kind !== 'queued' && item.kind !== 'running')
  const rows: QueueItem[] = [...active.sort(order), ...(all ? finished : finished.slice(0, FINISHED_SHOWN))]
  const { counts } = view
  return (
    <section className="cq" data-testid="clip-queue" aria-label={t('Clip queue')}>
      <div className="cq-head">
        <h2 className="cq-title">{t('Clip queue')}</h2>
        <div className="cq-counts">
          <span><b>{counts.running}</b> {t('running')}</span>
          <span><b>{counts.queued}</b> {t('waiting')}</span>
          <span><b>{counts.done}</b> {t('done')}</span>
          {counts.skipped > 0 && <span><b>{counts.skipped}</b> {t('skipped')}</span>}
          {counts.failed > 0 && <span><b>{counts.failed}</b> {t('failed')}</span>}
        </div>
      </div>
      <ul className="cq-list">
        {rows.map(item => (
          <li key={item.project_id} className="cq-row" role="link" tabIndex={0}
            onClick={() => navigate(`/project/${item.project_id}`)}
            onKeyDown={event => { if (event.key === 'Enter') navigate(`/project/${item.project_id}`) }}>
            <span className="cq-name" title={item.name}>{item.name}<span className="cq-time">{started(item.created_at)}</span></span>
            <span className={`cq-state${item.kind === 'failed' ? ' is-failed' : ''}${item.kind === 'done' ? ' is-done' : ''}`}>
              {t(STATE_LABEL[item.stage] || 'Waiting for a free slot')}{item.kind === 'running' ? ` · ${item.percent}%` : ''}
            </span>
            {(item.kind === 'running' || item.kind === 'queued') && (
              <span className={`cq-bar${item.kind === 'queued' ? ' is-queued' : ''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={item.percent}>
                <span style={{ width: `${Math.max(item.kind === 'queued' ? 3 : 6, item.percent)}%` }} />
              </span>
            )}
            {item.error && <span className="cq-note">{item.error}</span>}
          </li>
        ))}
      </ul>
      {finished.length > FINISHED_SHOWN && (
        <button type="button" className="studio-link cq-more" onClick={() => setAll(value => !value)}>
          {all ? t('Show fewer') : t('Show all finished ({{count}})', { count: finished.length })}
        </button>
      )}
    </section>
  )
}

const RANK: Record<string, number> = { running: 0, queued: 1 }
function order(a: QueueItem, b: QueueItem): number {
  return (RANK[a.kind] ?? 2) - (RANK[b.kind] ?? 2) || (a.created_at || '').localeCompare(b.created_at || '')
}
