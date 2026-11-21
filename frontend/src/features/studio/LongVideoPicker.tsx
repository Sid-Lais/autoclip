import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type RefObject } from 'react'
import { t } from '../../i18n'
import { Btn } from '../../ui'
import { studioApi, errorText, type ImportRangesFields, type VideoEmbed } from './api'
import { formatClock, parseClock, rangeProblem, MAX_RANGES, type PickedRange } from './clock'
import './long-video.css'

/** The slice of the YouTube IFrame API this picker uses. */
interface YouTubePlayer {
  getCurrentTime(): number
  seekTo(seconds: number, allowSeekAhead: boolean): void
  playVideo(): void
  pauseVideo(): void
  destroy(): void
}
interface YouTubeApi { Player: new (element: HTMLElement, options: Record<string, unknown>) => YouTubePlayer }
declare global {
  interface Window { YT?: YouTubeApi; onYouTubeIframeAPIReady?: () => void }
}

let youtubeApi: Promise<YouTubeApi> | null = null
function loadYouTubeApi(): Promise<YouTubeApi> {
  if (window.YT?.Player) return Promise.resolve(window.YT)
  if (!youtubeApi) {
    youtubeApi = new Promise<YouTubeApi>((resolve, reject) => {
      const previous = window.onYouTubeIframeAPIReady
      window.onYouTubeIframeAPIReady = () => { previous?.(); resolve(window.YT as YouTubeApi) }
      const script = document.createElement('script')
      script.src = 'https://www.youtube.com/iframe_api'
      script.async = true
      script.onerror = () => { youtubeApi = null; reject(new Error('The YouTube player could not be loaded.')) }
      document.head.appendChild(script)
    })
  }
  return youtubeApi
}

function bilibiliSrc(embed: VideoEmbed, start: number, autoplay: boolean) {
  const query = new URLSearchParams({ bvid: embed.id, p: String(embed.page || 1), autoplay: autoplay ? '1' : '0', high_quality: '1', t: String(Math.max(0, Math.floor(start))) })
  if (embed.id.startsWith('av')) { query.delete('bvid'); query.set('aid', embed.id.slice(2)) }
  return `https://player.bilibili.com/player.html?${query.toString()}`
}

/** One player for both sites. `readable` is false when the site's embed cannot report its playhead (Bilibili). */
function useOnlinePlayer(embed: VideoEmbed | null | undefined, host: RefObject<HTMLDivElement | null>, onTick: (time: number) => void) {
  const player = useRef<YouTubePlayer | null>(null)
  const [time, setTime] = useState(0)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState('')
  const [biliSrc, setBiliSrc] = useState(() => (embed?.site === 'bilibili' ? bilibiliSrc(embed, 0, false) : ''))
  const tick = useRef(onTick)
  tick.current = onTick

  useEffect(() => {
    if (embed?.site !== 'youtube' || !host.current) return
    let cancelled = false
    let timer = 0
    const mount = document.createElement('div')
    host.current.replaceChildren(mount)
    loadYouTubeApi().then(api => {
      if (cancelled) return
      player.current = new api.Player(mount, {
        videoId: embed.id,
        width: '100%',
        height: '100%',
        playerVars: { rel: 0, playsinline: 1, modestbranding: 1 },
        events: { onReady: () => { if (!cancelled) setReady(true) } },
      })
      timer = window.setInterval(() => {
        const current = player.current?.getCurrentTime?.()
        if (typeof current === 'number' && Number.isFinite(current)) { setTime(current); tick.current(current) }
      }, 250)
    }).catch(error => { if (!cancelled) setFailed(error instanceof Error ? error.message : String(error)) })
    return () => {
      cancelled = true
      window.clearInterval(timer)
      try { player.current?.destroy() } catch { /* The iframe is already gone. */ }
      player.current = null
      setReady(false)
    }
  }, [embed?.site, embed?.id, host])

  const seek = useCallback((seconds: number, play = false) => {
    if (embed?.site === 'youtube') {
      player.current?.seekTo(seconds, true)
      if (play) player.current?.playVideo()
      setTime(seconds)
    } else if (embed?.site === 'bilibili') {
      setBiliSrc(bilibiliSrc(embed, seconds, play))
    }
  }, [embed])
  const pause = useCallback(() => { player.current?.pauseVideo() }, [])
  return { readable: embed?.site === 'youtube', ready, failed, time, seek, pause, biliSrc }
}

function TimeField({ label, value, onChange, onUseCurrent, canUseCurrent }: {
  label: string; value: string; onChange: (value: string) => void; onUseCurrent: () => void; canUseCurrent: boolean
}) {
  return (
    <label className="lv-time">
      <span>{label}</span>
      <input value={value} inputMode="numeric" placeholder="0:00:00" aria-label={label} onChange={event => onChange(event.target.value)} />
      {canUseCurrent && <button type="button" className="studio-link" onClick={onUseCurrent}>{t('Use current time')}</button>}
    </label>
  )
}

export default function LongVideoPicker({ url, browser, title, duration, embed, fields, busy, onCreated, onWholeVideo }: {
  url: string
  browser: string
  title: string
  duration: number | null
  embed: VideoEmbed | null | undefined
  fields: ImportRangesFields
  busy?: boolean
  onCreated: (projectIds: string[], skipped: number) => void
  onWholeVideo: () => void
}) {
  const host = useRef<HTMLDivElement | null>(null)
  const [ranges, setRanges] = useState<PickedRange[]>([])
  const [startText, setStartText] = useState('')
  const [endText, setEndText] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [problem, setProblem] = useState('')
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [playing, setPlaying] = useState<string | null>(null)
  const stopAt = useRef<{ id: string; end: number } | null>(null)

  const player = useOnlinePlayer(embed, host, current => {
    const target = stopAt.current
    if (target && current >= target.end) {
      stopAt.current = null
      setPlaying(null)
      playerRef.current?.pause()
    }
  })
  const playerRef = useRef(player)
  playerRef.current = player

  const total = duration && duration > 0 ? duration : null
  const covered = useMemo(() => ranges.reduce((sum, range) => sum + (range.end - range.start), 0), [ranges])

  const submitRange = () => {
    const start = parseClock(startText)
    const end = parseClock(endText)
    if (start == null || end == null) { setProblem(t('Enter the start and end as h:mm:ss, for example 1:23:45.')); return }
    const found = rangeProblem({ start, end }, ranges, total, editing || undefined)
    if (found) { setProblem(t(found.key, found.values)); return }
    setProblem('')
    if (editing) {
      setRanges(prev => prev.map(range => range.id === editing ? { ...range, start, end } : range).sort((a, b) => a.start - b.start))
    } else {
      setRanges(prev => [...prev, { id: `r${Date.now().toString(36)}${prev.length}`, start, end, label: '' }].sort((a, b) => a.start - b.start))
    }
    setEditing(null)
    setStartText('')
    setEndText('')
  }
  const preview = (range: { id: string; start: number; end: number }) => {
    stopAt.current = player.readable ? { id: range.id, end: range.end } : null
    setPlaying(range.id)
    player.seek(range.start, true)
  }
  const edit = (range: PickedRange) => {
    setEditing(range.id)
    setStartText(formatClock(range.start))
    setEndText(formatClock(range.end))
    setProblem('')
  }
  const onTimeline = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!total) return
    const box = event.currentTarget.getBoundingClientRect()
    const fraction = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width))
    stopAt.current = null
    setPlaying(null)
    player.seek(fraction * total, player.readable)
  }
  const onTimelineKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!total || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) return
    event.preventDefault()
    player.seek(Math.min(total, Math.max(0, player.time + (event.key === 'ArrowRight' ? 10 : -10))))
  }

  const create = async () => {
    if (!ranges.length || submitting) return
    setSubmitting(true)
    setSubmitError('')
    try {
      const result = await studioApi.importRanges({
        ...fields,
        url,
        ...(browser ? { browser } : {}),
        title,
        ranges: ranges.map(({ start, end, label }) => ({ start, end, label: label.trim() })),
      })
      onCreated(result.projects.map(project => project.project_id), result.skipped)
    } catch (error) {
      setSubmitError(t(errorText(error)))
    } finally {
      setSubmitting(false)
    }
  }

  const disabled = !!busy || submitting
  return (
    <div className="lv" data-testid="long-video-picker">
      <p className="lv-intro">
        {t('This video is longer than 1.5 hours, so it is not downloaded. Watch it below, mark the parts you want, and only those parts are downloaded and turned into clips.')}
      </p>

      <div className="lv-stage">
        {embed?.site === 'bilibili' ? (
          <iframe className="lv-frame" src={player.biliSrc} title={t('视频预览')} allowFullScreen referrerPolicy="no-referrer" />
        ) : embed?.site === 'youtube' ? (
          <div className="lv-frame" ref={host} />
        ) : (
          <p className="studio-muted lv-nopreview">{t('This link has no online preview. Enter the times you want by hand.')}</p>
        )}
        {player.failed && <p className="studio-error" role="alert">{t(player.failed)}</p>}
      </div>

      {total && (
        <div className="lv-timeline-wrap">
          <div className="lv-timeline" role="slider" tabIndex={0} aria-label={t('Video timeline')}
            aria-valuemin={0} aria-valuemax={Math.round(total)} aria-valuenow={Math.round(player.time)}
            aria-valuetext={`${formatClock(player.time)} / ${formatClock(total)}`}
            onClick={onTimeline} onKeyDown={onTimelineKey}>
            {ranges.map(range => (
              <span key={range.id} className={`lv-seg${playing === range.id ? ' is-playing' : ''}`}
                style={{ left: `${(range.start / total) * 100}%`, width: `${Math.max(0.3, ((range.end - range.start) / total) * 100)}%` }}
                title={`${formatClock(range.start)} - ${formatClock(range.end)}`} />
            ))}
            {player.readable && <span className="lv-head" style={{ left: `${Math.min(100, (player.time / total) * 100)}%` }} />}
          </div>
          <div className="lv-clock">
            <span>{player.readable ? formatClock(player.time) : '—'}</span>
            <span>{formatClock(total)}</span>
          </div>
        </div>
      )}

      <div className="lv-editor">
        <TimeField label={t('Start')} value={startText} onChange={setStartText} onUseCurrent={() => setStartText(formatClock(player.time))} canUseCurrent={player.readable && player.ready} />
        <TimeField label={t('End')} value={endText} onChange={setEndText} onUseCurrent={() => setEndText(formatClock(player.time))} canUseCurrent={player.readable && player.ready} />
        <div className="lv-editor-actions">
          <Btn size="sm" variant="cta" disabled={disabled || !startText || !endText} onClick={submitRange}>
            {editing ? t('Update range') : t('Add range')}
          </Btn>
          {editing && <Btn size="sm" variant="text" onClick={() => { setEditing(null); setStartText(''); setEndText(''); setProblem('') }}>{t('取消')}</Btn>}
        </div>
      </div>
      {problem && <p className="studio-error" role="alert">{problem}</p>}

      <ul className="lv-list" aria-label={t('Picked ranges')}>
        {ranges.map((range, index) => (
          <li key={range.id} className={`lv-item${playing === range.id ? ' is-playing' : ''}`}>
            <span className="lv-item-index">{index + 1}</span>
            <div className="lv-item-main">
              <b>{formatClock(range.start)} - {formatClock(range.end)}</b>
              <span className="studio-muted">{formatClock(range.end - range.start)}</span>
              <input className="lv-label" value={range.label} maxLength={120} placeholder={t('Title (optional)')} aria-label={t('Clip title')}
                disabled={disabled} onChange={event => setRanges(prev => prev.map(item => item.id === range.id ? { ...item, label: event.target.value } : item))} />
            </div>
            <div className="lv-item-actions">
              <Btn size="sm" disabled={disabled} onClick={() => preview(range)}>{t('预览')}</Btn>
              <Btn size="sm" variant="text" disabled={disabled} onClick={() => edit(range)}>{t('编辑')}</Btn>
              <Btn size="sm" variant="text" disabled={disabled} onClick={() => setRanges(prev => prev.filter(item => item.id !== range.id))}>{t('移除')}</Btn>
            </div>
          </li>
        ))}
      </ul>
      {!player.readable && ranges.length > 0 && embed?.site === 'bilibili' && (
        <p className="studio-muted">{t('Bilibili\'s player cannot stop at the end of a range, so Preview starts playing at the start time.')}</p>
      )}

      <div className="studio-actions lv-footer">
        <Btn variant="cta" disabled={disabled || !ranges.length} loading={submitting} onClick={() => void create()}>
          {ranges.length
            ? t('Download {{count}} part(s) and make clips', { count: ranges.length })
            : t('Add at least one range')}
        </Btn>
        {ranges.length > 0 && (
          <span className="studio-muted">{t('{{minutes}} min of video will be downloaded (limit {{max}} ranges).', { minutes: Math.max(1, Math.round(covered / 60)), max: MAX_RANGES })}</span>
        )}
        <Btn size="sm" variant="text" disabled={disabled} onClick={onWholeVideo}>{t('Download the whole video instead')}</Btn>
      </div>
      {submitError && <p className="studio-error" role="alert">{submitError}</p>}
    </div>
  )
}
