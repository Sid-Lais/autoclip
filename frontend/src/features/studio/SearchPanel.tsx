import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getLocale, t } from '../../i18n'
import { Btn } from '../../ui'
import { studioApi, errorText, type ImportRangesFields, type SearchResult, type SuggestedClips } from './api'
import { formatClock } from './clock'
import './long-video.css'

type Suggestion = { state: 'loading' } | { state: 'error'; message: string } | { state: 'ready'; data: SuggestedClips }
type Row = { checked: boolean; suggestion?: Suggestion; off: Set<number> }

const MAX_APPROVED = 10
const SUGGEST_CONCURRENCY = 3

function views(count: number | null): string {
  return count == null ? '' : new Intl.NumberFormat(getLocale(), { notation: 'compact', maximumFractionDigits: 1 }).format(count)
}

/** Search for videos, approve the ones to clip, and make clips from the parts viewers replay most. */
export default function SearchPanel({ browser, fields, busy, onCreated, onPickParts }: {
  browser: string
  fields: ImportRangesFields
  busy?: boolean
  onCreated: (projectIds: string[], skipped: number) => void
  /** Hand a long video to the range picker (the link flow) when nothing suggests which parts to cut. */
  onPickParts: (url: string) => void
}) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [rows, setRows] = useState<Record<string, Row>>({})
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)
  const [creating, setCreating] = useState(false)
  const [gameplayOnly, setGameplayOnly] = useState(true)
  const [error, setError] = useState('')
  const generation = useRef(0)
  const queue = useRef<string[]>([])
  const running = useRef(0)

  const patch = useCallback((url: string, change: (row: Row) => Row) => {
    setRows(prev => ({ ...prev, [url]: change(prev[url] ?? { checked: false, off: new Set() }) }))
  }, [])

  const pump = useCallback((token: number) => {
    while (running.current < SUGGEST_CONCURRENCY && queue.current.length) {
      const url = queue.current.shift() as string
      running.current += 1
      patch(url, row => ({ ...row, suggestion: { state: 'loading' } }))
      studioApi.suggestClips({ url, ...(browser ? { browser } : {}) })
        .then(data => { if (token === generation.current) patch(url, row => ({ ...row, suggestion: { state: 'ready', data } })) })
        .catch(failure => { if (token === generation.current) patch(url, row => ({ ...row, suggestion: { state: 'error', message: t(errorText(failure)) } })) })
        .finally(() => { running.current -= 1; if (token === generation.current) pump(token) })
    }
  }, [browser, patch])

  const search = async () => {
    const text = query.trim()
    if (text.length < 2 || searching) return
    generation.current += 1
    const token = generation.current
    queue.current = []
    setSearching(true)
    setError('')
    try {
      const found = (await studioApi.searchVideos({ query: text, limit: 12 })).results
      if (token !== generation.current) return
      setResults(found)
      setSearched(true)
      setRows(Object.fromEntries(found.map(item => [item.url, { checked: item.suggested, off: new Set<number>() }])))
      queue.current = found.filter(item => item.suggested).map(item => item.url)
      pump(token)
    } catch (failure) {
      if (token === generation.current) setError(t(errorText(failure)))
    } finally {
      if (token === generation.current) setSearching(false)
    }
  }

  useEffect(() => () => { generation.current += 1 }, [])

  const toggle = (item: SearchResult) => {
    const row = rows[item.url]
    const checked = !(row?.checked)
    patch(item.url, current => ({ ...current, checked }))
    if (checked && !row?.suggestion) { queue.current.push(item.url); pump(generation.current) }
  }

  const plan = useMemo(() => results.flatMap(item => {
    const row = rows[item.url]
    const data = row?.suggestion?.state === 'ready' ? row.suggestion.data : null
    if (!row?.checked || !data) return []
    if (data.mode === 'ranges') {
      const ranges = data.moments.filter((_, index) => !row.off.has(index)).map(moment => ({ start: moment.start, end: moment.end, label: moment.label || '' }))
      return ranges.length ? [{ url: item.url, title: item.title, ranges }] : []
    }
    return data.mode === 'whole' ? [{ url: item.url, title: item.title, ranges: [] }] : []
  }), [results, rows])
  const clipCount = plan.reduce((sum, item) => sum + Math.max(1, item.ranges.length), 0)
  const disabled = !!busy || creating

  const create = async () => {
    if (!plan.length || creating) return
    setCreating(true)
    setError('')
    try {
      const result = await studioApi.autoClip({ ...fields, ...(gameplayOnly ? { visual_focus: 'gameplay' as const } : {}), ...(browser ? { browser } : {}), items: plan.slice(0, MAX_APPROVED) })
      onCreated(result.projects.map(project => project.project_id), result.skipped.length)
    } catch (failure) {
      setError(t(errorText(failure)))
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="lv sp" data-testid="search-panel">
      <p className="lv-intro">{t('Search YouTube. The most viewed videos are suggested; approve the ones you want and clips are made from the parts viewers replay most.')}</p>
      <label className="sp-row" style={{ cursor: 'pointer' }}>
        <input type="checkbox" checked={gameplayOnly} disabled={disabled} onChange={event => setGameplayOnly(event.target.checked)} />
        <span className="sp-meta"><b>{t('Gameplay only')}</b><span className="studio-muted">{t('The AI looks at the frames and keeps parts with real gameplay. Face-cam reactions around the gameplay stay. Trailer cuts and menus are dropped, and parts with no gameplay are skipped.')}</span></span>
      </label>
      <form className="sp-form" onSubmit={event => { event.preventDefault(); void search() }}>
        <input className="lv-label" value={query} maxLength={200} disabled={disabled} aria-label={t('Search query')}
          placeholder={t('For example: AI podcast interview')} onChange={event => setQuery(event.target.value)} />
        <Btn variant="cta" size="sm" loading={searching} disabled={disabled || query.trim().length < 2} onClick={() => void search()}>{t('Find videos')}</Btn>
      </form>
      {error && <p className="studio-error" role="alert">{error}</p>}
      {searched && !results.length && <p className="studio-muted">{t('No videos found. Try different words.')}</p>}

      <ul className="lv-list" aria-label={t('Search results')}>
        {results.map(item => {
          const row = rows[item.url]
          const suggestion = row?.suggestion
          return (
            <li key={item.url} className="sp-item">
              <label className="sp-row">
                <input type="checkbox" checked={!!row?.checked} disabled={disabled} onChange={() => toggle(item)} aria-label={item.title} />
                {item.thumbnail ? <img className="sp-thumb" src={item.thumbnail} alt="" referrerPolicy="no-referrer" /> : <span className="sp-thumb" />}
                <span className="sp-meta">
                  <b>{item.title}</b>
                  <span className="studio-muted">
                    {[item.channel, item.view_count != null ? t('{{views}} views', { views: views(item.view_count) }) : '', formatClock(item.duration)].filter(Boolean).join(' · ')}
                    {item.suggested ? ` · ${t('Suggested')}` : ''}
                  </span>
                </span>
              </label>
              {row?.checked && (
                <div className="sp-detail">
                  {(!suggestion || suggestion.state === 'loading') && <span className="studio-muted">{t('Looking for the most replayed moments…')}</span>}
                  {suggestion?.state === 'error' && <span className="studio-error">{suggestion.message}</span>}
                  {suggestion?.state === 'ready' && suggestion.data.mode === 'ranges' && (
                    <ul className="sp-moments" aria-label={t('Suggested parts')}>
                      {suggestion.data.moments.map((moment, index) => (
                        <li key={`${moment.start}-${moment.end}`}>
                          <label>
                            <input type="checkbox" checked={!row.off.has(index)} disabled={disabled}
                              onChange={() => patch(item.url, current => { const off = new Set(current.off); if (off.has(index)) off.delete(index); else off.add(index); return { ...current, off } })} />
                            <span>{formatClock(moment.start)} - {formatClock(moment.end)}</span>
                            <span className="studio-muted">
                              {moment.source === 'most_replayed' && moment.score != null
                                ? t('Most replayed ({{percent}}%)', { percent: Math.round(moment.score * 100) })
                                : moment.label || t('Chapter')}
                            </span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  )}
                  {suggestion?.state === 'ready' && suggestion.data.mode === 'whole' && (
                    <span className="studio-muted">{t('No replay data for this video. The AI picks the best parts from the whole video.')}</span>
                  )}
                  {suggestion?.state === 'ready' && suggestion.data.mode === 'manual' && (
                    <span className="studio-muted">
                      {t('This video is over 1.5 hours and has no replay data.')}{' '}
                      <button type="button" className="studio-link" disabled={disabled} onClick={() => onPickParts(item.url)}>{t('Pick the parts yourself')}</button>
                    </span>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>

      {results.length > 0 && (
        <div className="studio-actions lv-footer">
          <Btn variant="cta" loading={creating} disabled={disabled || !plan.length} onClick={() => void create()}>
            {plan.length ? t('Make {{count}} clip(s) from {{videos}} video(s)', { count: clipCount, videos: plan.length }) : t('Approve at least one video')}
          </Btn>
          <span className="studio-muted">{t('Videos with replay data are cut to the approved parts. The rest are downloaded whole.')}</span>
        </div>
      )}
    </div>
  )
}
