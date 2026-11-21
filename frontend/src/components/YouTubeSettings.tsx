import { t } from '../i18n'
import { useTranslation } from 'react-i18next'
import React, { useCallback, useEffect, useState } from 'react'
import { message } from 'antd'
import { Btn, Row, Section, Segmented, StatusDot } from '../ui'
import api from '../services/api'
import { openExternalLink } from '../utils/externalLinks'
import { readApiDetail } from '../publish/uploadPost'

type Privacy = 'private' | 'unlisted' | 'public'
interface YouTubeView {
  has_client: boolean
  client_id: string
  connected: boolean
  channel_title: string
  privacy: Privacy
  auto_post: boolean
  strategies: string[]
  redirect_uri: string
}

const CONSOLE_URL = 'https://console.cloud.google.com/apis/library/youtube.googleapis.com'
const CREDENTIALS_URL = 'https://console.cloud.google.com/apis/credentials'

const youtubeApi = {
  get: (): Promise<YouTubeView> => api.get('/publish/youtube/config'),
  save: (body: Partial<Pick<YouTubeView, 'privacy' | 'auto_post'>> & { client_id?: string; client_secret?: string }): Promise<YouTubeView> => api.put('/publish/youtube/config', body),
  complete: (url: string): Promise<YouTubeView> => api.post('/publish/youtube/complete', { url }),
  disconnect: (): Promise<YouTubeView> => api.delete('/publish/youtube/config'),
  authUrl: (): Promise<{ url: string }> => api.get('/publish/youtube/auth-url'),
}

/** Connect your own YouTube channel (free: your own Google Cloud OAuth client) and post finished clips automatically. */
const YouTubeSettings: React.FC = () => {
  useTranslation()
  const [view, setView] = useState<YouTubeView | null>(null)
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [signInLink, setSignInLink] = useState('')
  const [pasted, setPasted] = useState('')

  const load = useCallback(async () => {
    try {
      const next = await youtubeApi.get()
      setView(next)
      setClientId(next.client_id)
    } catch (err) {
      setError(readApiDetail(err, t('The YouTube settings could not be loaded.')))
    }
  }, [])

  useEffect(() => {
    void load()
    const result = new URLSearchParams(window.location.hash.split('?')[1] || '').get('youtube')
    if (result === 'connected') void message.success(t('YouTube connected.'))
    else if (result) setError(t('Google sign-in did not finish. Check the client id and secret and the redirect address, then try again.'))
  }, [load])

  const run = async (work: () => Promise<void>) => {
    setBusy(true)
    setError('')
    try { await work() } catch (err) { setError(readApiDetail(err, t('The YouTube request failed.'))) } finally { setBusy(false) }
  }

  const saveClient = () => run(async () => {
    setView(await youtubeApi.save({ client_id: clientId.trim(), ...(clientSecret.trim() ? { client_secret: clientSecret.trim() } : {}) }))
    setClientSecret('')
    void message.success(t('Saved'))
  })
  const connect = () => run(async () => {
    const { url } = await youtubeApi.authUrl()
    window.location.href = url
  })
  const makeLink = () => run(async () => {
    const { url } = await youtubeApi.authUrl()
    setSignInLink(url)
    try { await navigator.clipboard.writeText(url); void message.success(t('Sign-in link copied.')) } catch { /* the link is shown below to copy by hand */ }
  })
  const finish = () => run(async () => {
    setView(await youtubeApi.complete(pasted.trim()))
    setPasted('')
    setSignInLink('')
    void message.success(t('YouTube connected.'))
  })
  const change = (patch: Parameters<typeof youtubeApi.save>[0]) => run(async () => { setView(await youtubeApi.save(patch)) })
  const disconnect = () => run(async () => { setView(await youtubeApi.disconnect()); void message.success(t('YouTube disconnected.')) })

  const link = (href: string, text: string) => (
    <a href={href} onClick={(e) => { e.preventDefault(); void openExternalLink(href) }} style={{ color: 'var(--ac-accent)' }}>{text}</a>
  )

  return (
    <>
      <Section title={t('YouTube')} description={t('Post finished clips to your own channel with their generated title, description and tags. Free: it uses your own Google Cloud project.')}>
        <div className="ac-rows">
          <Row wide label={t('OAuth client id')} hint={<>
            {t('Google Cloud: enable "YouTube Data API v3", then create an OAuth client of type "Web application".')}{' '}
            {link(CONSOLE_URL, t('Enable the API'))} · {link(CREDENTIALS_URL, t('Create the client'))}
          </>}>
            <input className="ac-input ac-input--mono" spellCheck={false} aria-label={t('OAuth client id')} value={clientId} onChange={(e) => setClientId(e.target.value)} />
          </Row>
          <Row wide label={t('OAuth client secret')} hint={view?.has_client ? t('A secret is saved. Leave empty to keep it.') : undefined}>
            <input className="ac-input ac-input--mono" type="password" autoComplete="new-password" spellCheck={false} aria-label={t('OAuth client secret')}
              placeholder={view?.has_client ? '••••••••' : ''} value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
          </Row>
          <Row label={t('Redirect address')} hint={t('Add this exact address to "Authorized redirect URIs" of the client, and open AutoClip at http://localhost:3000 on this PC to connect.')}>
            <code style={{ fontSize: 12, overflowWrap: 'anywhere' }}>{view?.redirect_uri || ''}</code>
          </Row>
          <Row label={t('Client')}>
            <Btn size="sm" variant="cta" loading={busy} disabled={!clientId.trim() || (!view?.has_client && !clientSecret.trim())} onClick={() => void saveClient()}>{t('Save client')}</Btn>
          </Row>
          <Row label={t('Status')}>
            {view?.connected
              ? <StatusDot tone="ok" label={view.channel_title ? t('Connected: {{name}}', { name: view.channel_title }) : t('Connected')} />
              : <StatusDot tone="muted" label={t('Not connected')} />}
            {view?.connected
              ? <Btn size="sm" variant="danger" loading={busy} onClick={() => void disconnect()}>{t('Disconnect')}</Btn>
              : <Btn size="sm" variant="cta" loading={busy} disabled={!view?.has_client} onClick={() => void connect()}>{t('Connect YouTube')}</Btn>}
          </Row>
          {!view?.connected && view?.has_client && <Row wide stack label={t('Someone else signs in')} hint={t('If the channel owner signs in on their own device: send them the link, they sign in, and Google then shows a page that does not open. They copy that page\'s full address and send it back; paste it here.')}>
            <Btn size="sm" loading={busy} onClick={() => void makeLink()}>{t('Copy sign-in link')}</Btn>
            {signInLink && <textarea className="ac-input ac-input--mono" readOnly rows={3} aria-label={t('Sign-in link')} value={signInLink} onFocus={(e) => e.currentTarget.select()} />}
            <input className="ac-input ac-input--mono" spellCheck={false} aria-label={t('Address from the owner')} placeholder="http://localhost:3000/api/v1/publish/youtube/callback?state=…&code=…" value={pasted} onChange={(e) => setPasted(e.target.value)} />
            <Btn size="sm" variant="cta" loading={busy} disabled={!pasted.trim()} onClick={() => void finish()}>{t('Finish connecting')}</Btn>
          </Row>}
          {view?.connected && <>
            <Row stack label={t('Visibility of uploads')} hint={t('Private lets you review each video in YouTube Studio before it goes public. YouTube may force private for apps Google has not audited.')}>
              <Segmented className="ac-segmented" size="sm" ariaLabel={t('Visibility of uploads')} value={view.privacy} disabled={busy}
                onChange={(privacy) => void change({ privacy })}
                options={[{ value: 'private', label: t('Private') }, { value: 'unlisted', label: t('Unlisted') }, { value: 'public', label: t('Public') }]} />
            </Row>
            <Row label={t('Post automatically')} hint={t('Every finished YouTube Shorts / YouTube video clip is uploaded as soon as it is ready.')}>
              <Segmented className="ac-segmented" size="sm" ariaLabel={t('Post automatically')} value={view.auto_post ? 'on' : 'off'} disabled={busy}
                onChange={(value) => void change({ auto_post: value === 'on' })}
                options={[{ value: 'off', label: t('Off') }, { value: 'on', label: t('On') }]} />
            </Row>
          </>}
        </div>
      </Section>
      {error && <p style={{ marginTop: 12, color: 'var(--ac-error)', fontSize: 13 }}>{error}</p>}
    </>
  )
}

export default YouTubeSettings
