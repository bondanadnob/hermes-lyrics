import {
  PALETTE_AREA,
  PANES_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  SegmentedControl,
  cn,
  haptic,
  host,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'apple-music-lyrics'
const ROUTE = '/apple-music-lyrics'
const PROFILE_CHANGED_ERROR = 'Active Hermes profile changed during Apple Music request'

const queryKey = (ctx, profile) => [ID, ctx.source, profile || 'default', 'state']
const artworkProfileQueryKey = (ctx, profile) => [
  ID,
  ctx.source,
  profile || 'default',
  'artwork'
]
const artworkQueryKey = (ctx, profile, identity) => [
  ...artworkProfileQueryKey(ctx, profile),
  identity || 'none'
]
const LOCAL_ARTWORK_PATTERN = new RegExp(
  '^data:image/(?:jpeg|png);base64,[A-Za-z0-9+/]+={0,2}$'
)
const assertActiveProfile = profile => {
  if (host.state.profile.get() !== profile) {
    throw new Error(PROFILE_CHANGED_ERROR)
  }
}
const clamp = (value, minimum, maximum) =>
  Math.min(maximum, Math.max(minimum, value))

function formatTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0))
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`
}

function findCurrentIndex(lines, position) {
  if (!Array.isArray(lines) || !lines.length) return -1
  let low = 0
  let high = lines.length
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if ((Number(lines[middle]?.time) || 0) <= position) low = middle + 1
    else high = middle
  }
  return low - 1
}

function activeLineFor(state, position) {
  const lyrics = state?.lyrics
  if (!lyrics?.synced || !Array.isArray(lyrics.lines)) return null
  const index = findCurrentIndex(lyrics.lines, position)
  return index >= 0 ? lyrics.lines[index] || null : null
}

function useMusicState(ctx, profile) {
  return useQuery({
    queryKey: queryKey(ctx, profile),
    queryFn: async () => {
      assertActiveProfile(profile)
      const state = await ctx.rest('/state', { timeoutMs: 20000 })
      assertActiveProfile(profile)
      return state
    },
    refetchInterval: 2000,
    staleTime: 750,
    retry: false
  })
}

function useMusicArtwork(ctx, profile, identity, enabled) {
  return useQuery({
    queryKey: artworkQueryKey(ctx, profile, identity),
    queryFn: async () => {
      assertActiveProfile(profile)
      const response = await ctx.rest(
        `/artwork?identity=${encodeURIComponent(identity)}`,
        { timeoutMs: 12000 }
      )
      assertActiveProfile(profile)
      const artwork = response?.artwork
      if (
        !artwork ||
        typeof artwork !== 'object' ||
        artwork.identity !== identity ||
        typeof artwork.data_url !== 'string' ||
        !artwork.data_url.startsWith('data:image/') ||
        safeArtworkUrl(artwork.data_url) !== artwork.data_url
      ) {
        throw new Error('Invalid artwork response from Apple Music Lyrics backend')
      }
      return artwork
    },
    enabled: Boolean(identity && enabled),
    staleTime: 300000,
    gcTime: 10000,
    retry: artworkRetry,
    retryOnMount: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retryDelay: attempt => Math.min(4000, 2000 * 2 ** attempt)
  })
}

function useInterpolatedPosition(track, animate = true) {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (track?.state !== 'playing' || !animate) return undefined
    const timer = window.setInterval(() => setTick(value => value + 1), 100)
    return () => window.clearInterval(timer)
  }, [animate, track?.state, track?.sampled_at, track?.position])

  return useMemo(() => {
    void tick
    if (!track) return 0
    const sampled = Number(track.sampled_at) || Date.now() / 1000
    const base = Number(track.position) || 0
    const elapsed =
      track.state === 'playing' && animate ? Date.now() / 1000 - sampled : 0
    return clamp(base + Math.max(0, elapsed), 0, Number(track.duration) || Infinity)
  }, [animate, tick, track])
}

function ActionButton({ label, children, disabled, onClick }) {
  return jsx('button', {
    'aria-label': label,
    title: label,
    type: 'button',
    disabled,
    onClick,
    className: cn(
      'inline-flex h-8 items-center justify-center rounded-md px-2 text-sm',
      'text-(--ui-text-secondary) transition-colors',
      'hover:bg-(--chrome-action-hover) hover:text-foreground',
      'disabled:pointer-events-none disabled:opacity-40'
    ),
    style: { minWidth: '2rem' },
    children
  })
}

function SourceSwitcher({ disabled, onChange, source }) {
  return jsx(SegmentedControl, {
    disabled,
    onChange,
    options: [
      { id: 'music_app', label: 'Music.app' },
      { id: 'ambient', label: 'Nearby' }
    ],
    value: source === 'ambient' ? 'ambient' : 'music_app'
  })
}

function safeArtworkUrl(value) {
  if (typeof value !== 'string' || !value) return null
  if (value.length <= 2700000 && LOCAL_ARTWORK_PATTERN.test(value)) {
    const payload = value.slice(value.indexOf(',') + 1)
    if (payload.length % 4 !== 0) return null
    const padding = payload.endsWith('==') ? 2 : payload.endsWith('=') ? 1 : 0
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    if (padding === 1 && alphabet.indexOf(payload.at(-2)) % 4 !== 0) return null
    if (padding === 2 && alphabet.indexOf(payload.at(-3)) % 16 !== 0) return null
    const decodedBytes = (payload.length / 4) * 3 - padding
    return decodedBytes <= 2000000 ? value : null
  }
  if (value.length > 2048) return null
  try {
    const parsed = new URL(value)
    const hostname = parsed.hostname.toLowerCase()
    if (
      parsed.protocol === 'https:' &&
      !parsed.username &&
      !parsed.password &&
      (parsed.port === '' || parsed.port === '443') &&
      (hostname === 'mzstatic.com' || hostname.endsWith('.mzstatic.com'))
    ) {
      return parsed.href
    }
  } catch (_) {}
  return null
}

function restErrorStatus(error) {
  const message = error instanceof Error ? error.message : String(error || '')
  let status = null
  for (const match of message.matchAll(/(?:^|:\s)([45]\d{2}):(?=\s|$)/g)) {
    status = Number(match[1])
  }
  return status
}

function artworkRetry(failureCount, error) {
  return failureCount < 2 && restErrorStatus(error) === 503
}

function selectArtworkSources(artworkQuery, remoteUrl, validatedLocalUrl) {
  const localUrl = validatedLocalUrl ?? safeArtworkUrl(artworkQuery?.data?.data_url)
  const remote = safeArtworkUrl(remoteUrl)
  const settled = Boolean(artworkQuery?.isSuccess || artworkQuery?.isError)
  if (!settled) return { url: null, fallbackUrl: null }
  const errorMessage =
    artworkQuery?.error instanceof Error
      ? artworkQuery.error.message
      : String(artworkQuery?.error || '')
  if (
    restErrorStatus(artworkQuery?.error) === 409 ||
    errorMessage === PROFILE_CHANGED_ERROR
  ) {
    return { url: null, fallbackUrl: null }
  }
  if (artworkQuery?.isError) return { url: remote, fallbackUrl: null }
  if (localUrl) return { url: localUrl, fallbackUrl: remote }
  return { url: remote, fallbackUrl: null }
}

function Artwork({ url, fallbackUrl, compact }) {
  const safeUrl = useMemo(() => safeArtworkUrl(url), [url])
  const safeFallbackUrl = useMemo(() => safeArtworkUrl(fallbackUrl), [fallbackUrl])
  const candidates = useMemo(
    () => [safeUrl, safeFallbackUrl].filter((value, index, all) => value && all.indexOf(value) === index),
    [safeFallbackUrl, safeUrl]
  )
  const [failed, setFailed] = useState([])
  const activeUrl = candidates.find(candidate => !failed.includes(candidate))
  const size = compact ? '3.5rem' : '5rem'
  const style = { height: size, width: size }
  useEffect(() => setFailed([]), [candidates])
  if (activeUrl) {
    return jsx('img', {
      src: activeUrl,
      alt: '',
      loading: 'lazy',
      decoding: 'async',
      referrerPolicy: 'no-referrer',
      onError: () => setFailed(current => [...current, activeUrl]),
      className: 'shrink-0 rounded-xl object-cover shadow-sm',
      style
    })
  }
  return jsx('div', {
    'aria-hidden': true,
    className: cn(
      'flex shrink-0 items-center justify-center rounded-xl border',
      'border-(--ui-stroke-secondary) text-2xl text-(--ui-text-tertiary)'
    ),
    style,
    children: '♪'
  })
}

function SourcePill({ lyrics }) {
  const label = lyrics?.source || 'Lyrics unavailable'
  const detail = lyrics?.word_timing === 'exact' ? ' · word timed' : lyrics?.synced ? ' · synced' : ''
  return jsx('span', {
    className: cn(
      'inline-flex max-w-full items-center truncate rounded-full border px-2 py-0.5',
      'border-(--ui-stroke-secondary) text-[0.65rem] text-(--ui-text-tertiary)'
    ),
    children: `${label}${detail}`
  })
}

function PlayerHeader({ artworkUrl, fallbackUrl, compact, lyrics, onSourceChange, position, refresh, runAction, runSeek, source, track, working }) {
  const percentage = track.duration
    ? clamp((position / track.duration) * 100, 0, 100)
    : 0
  const subtitle = [track.artist, track.album].filter(Boolean).join(' · ')
  const canControl = track.can_control !== false
  const canSeek = track.can_seek !== false

  const seekFromEvent = event => {
    if (!canSeek || !track.duration || working) return
    const rectangle = event.currentTarget.getBoundingClientRect()
    const ratio = clamp((event.clientX - rectangle.left) / rectangle.width, 0, 1)
    runSeek(ratio * track.duration)
  }
  const seekFromKeyboard = event => {
    if (!canSeek || !track.duration || working) return
    const steps = {
      ArrowLeft: position - 5,
      ArrowDown: position - 5,
      ArrowRight: position + 5,
      ArrowUp: position + 5,
      Home: 0,
      End: track.duration
    }
    if (!(event.key in steps)) return
    event.preventDefault()
    runSeek(clamp(steps[event.key], 0, track.duration))
  }

  return jsxs('div', {
    className: 'shrink-0 border-b border-(--ui-stroke-secondary) p-3',
    children: [
      jsx('div', {
        className: 'mb-3 flex justify-center',
        children: jsx(SourceSwitcher, {
          disabled: working,
          onChange: onSourceChange,
          source
        })
      }),
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-3',
        children: [
          jsx(Artwork, { url: artworkUrl, fallbackUrl, compact }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: cn(
                  'truncate font-semibold text-foreground',
                  compact ? 'text-sm' : 'text-base'
                ),
                title: track.title,
                children: track.title || 'Apple Music'
              }),
              jsx('div', {
                className: 'mt-0.5 truncate text-xs text-(--ui-text-secondary)',
                title: subtitle,
                children: subtitle || 'Nothing playing'
              }),
              jsx('div', {
                className: 'mt-1.5',
                children: jsx(SourcePill, { lyrics })
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'mt-3 flex items-center justify-center gap-1',
        children: [
          canControl &&
            jsx(ActionButton, {
              label: 'Previous track',
              disabled: working,
              onClick: () => runAction('previous'),
              children: '◀◀'
            }),
          canControl &&
            jsx(ActionButton, {
              label: track.state === 'playing' ? 'Pause' : 'Play',
              disabled: working,
              onClick: () => runAction('play_pause'),
              children: track.state === 'playing' ? '❚❚' : '▶'
            }),
          canControl &&
            jsx(ActionButton, {
              label: 'Next track',
              disabled: working,
              onClick: () => runAction('next'),
              children: '▶▶'
            }),
          jsx(ActionButton, {
            label: 'Refresh lyrics and artwork',
            disabled: working,
            onClick: refresh,
            children: '↻'
          })
        ]
      }),
      track.duration > 0 &&
        jsx('div', {
          role: canSeek ? 'slider' : 'progressbar',
        tabIndex: canSeek && track.duration && !working ? 0 : -1,
        'aria-label': canSeek ? 'Seek in current track' : 'Estimated nearby playback position',
        'aria-disabled': !canSeek || !track.duration || working,
        'aria-valuemin': 0,
        'aria-valuemax': Math.round(Number(track.duration) || 0),
        'aria-valuenow': Math.round(position),
        'aria-valuetext': `${formatTime(position)} of ${formatTime(track.duration)}`,
        onClick: canSeek ? seekFromEvent : undefined,
        onKeyDown: canSeek ? seekFromKeyboard : undefined,
        className: cn(
          'mt-2 block h-2 w-full rounded-full bg-(--ui-stroke-secondary)',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
          !canSeek || working ? 'cursor-default opacity-60' : 'cursor-pointer'
        ),
        children: jsx('span', {
          className: 'block h-full rounded-full bg-(--ui-accent) transition-[width] duration-100',
          style: { width: `${percentage}%` }
        })
      }),
      track.duration > 0 &&
        jsxs('div', {
          className: 'mt-1 flex justify-between text-[0.65rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { children: formatTime(position) }),
          jsx('span', { children: formatTime(track.duration) })
        ]
      })
    ]
  })
}

function progressiveStyle(progress) {
  const percentage = clamp(progress * 100, 0, 100)
  return {
    backgroundImage: `linear-gradient(90deg, var(--ui-accent) ${percentage}%, var(--ui-text-secondary) ${percentage}%)`,
    backgroundClip: 'text',
    WebkitBackgroundClip: 'text',
    color: 'transparent'
  }
}

function ExactWords({ line, position }) {
  return (line.words || []).map((word, index) => {
    const start = Number(word.start) || 0
    const end = Math.max(Number(word.end) || start + 0.01, start + 0.01)
    const progress = (position - start) / (end - start)
    return jsx(
      'span',
      {
        style: progressiveStyle(progress),
        children: word.text
      },
      `${start}-${index}`
    )
  })
}

function ActiveLyric({ line, lines, index, position }) {
  if (!line?.text) {
    return jsx('span', {
      className: 'text-(--ui-text-tertiary)',
      children: '♪ Instrumental'
    })
  }
  if (Array.isArray(line.words) && line.words.length && line.words.some(word => word.start != null)) {
    return jsx('span', { children: jsx(ExactWords, { line, position }) })
  }
  const next = Number(line.end) || Number(lines[index + 1]?.time) || position + 4
  const duration = Math.max(0.1, next - (Number(line.time) || 0))
  return jsx('span', {
    style: progressiveStyle((position - Number(line.time || 0)) / duration),
    children: line.text
  })
}

function LyricsScroller({ canSeek, compact, lyrics, position, seek, working }) {
  const lines = Array.isArray(lyrics?.lines) ? lyrics.lines : []
  const currentIndex = lyrics?.synced ? findCurrentIndex(lines, position) : -1
  const activeRef = useRef(null)
  const [follow, setFollow] = useState(true)

  useEffect(() => {
    if (!follow || !activeRef.current) return
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    activeRef.current.scrollIntoView({
      block: 'center',
      behavior: reduceMotion ? 'auto' : 'smooth'
    })
  }, [currentIndex, follow])

  if (!lines.length) {
    return jsx('div', {
      className: 'flex h-full items-center justify-center p-6 text-center text-sm text-(--ui-text-tertiary)',
      children: 'No lyrics are available for this track yet.'
    })
  }

  return jsxs('div', {
    className: 'relative min-h-0 flex-1',
    children: [
      jsx('div', {
        className: 'flex h-full flex-col overflow-y-auto px-4',
        style: {
          gap: compact ? '1.25rem' : '1.75rem',
          paddingTop: '42%',
          paddingBottom: '42%'
        },
        onWheel: () => setFollow(false),
        onTouchStart: () => setFollow(false),
        children: lines.map((line, index) => {
          const current = index === currentIndex
          const distance = currentIndex < 0 ? 4 : Math.abs(index - currentIndex)
          const opacity = !lyrics.synced
            ? 0.82
            : current
              ? 1
              : distance <= 1
                ? 0.68
                : distance <= 3
                  ? 0.4
                  : 0.24
          return jsx(
            'button',
            {
              ref: current ? activeRef : undefined,
              type: 'button',
              disabled: !canSeek || !lyrics.synced || working,
              onClick: () => {
                if (canSeek && lyrics.synced && !working) {
                  haptic('tap')
                  seek(Number(line.time) || 0)
                  setFollow(true)
                }
              },
              className: cn(
                'block w-full whitespace-pre-wrap text-left leading-snug transition-all duration-300',
                current
                  ? compact
                    ? 'text-lg font-semibold'
                    : 'text-2xl font-semibold'
                  : compact
                    ? 'text-sm text-(--ui-text-secondary)'
                    : 'text-lg text-(--ui-text-secondary)',
                canSeek && lyrics.synced && !working ? 'cursor-pointer' : 'cursor-default'
              ),
              style: { opacity },
              children: current
                ? jsx(ActiveLyric, { line, lines, index, position })
                : line.text || '♪'
            },
            `${Number(line.time) || index}-${index}`
          )
        })
      }),
      !follow &&
        jsx('button', {
          type: 'button',
          onClick: () => setFollow(true),
          className: cn(
            'absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border px-3 py-1.5 text-xs shadow-sm',
            'border-(--ui-stroke-secondary) text-(--ui-text-secondary)',
            'hover:text-foreground'
          ),
          style: { backgroundColor: 'var(--ui-bg-primary)' },
          children: 'Resume following'
        })
    ]
  })
}

const MemoizedLyricsScroller = memo(
  LyricsScroller,
  (previous, next) =>
    previous.canSeek === next.canSeek &&
    previous.compact === next.compact &&
    previous.lyrics === next.lyrics &&
    previous.position === next.position &&
    previous.working === next.working
)

function StateNotice({ listen, onSourceChange, status, stop, refresh, openPermissions, source, working }) {
  const common = 'flex h-full flex-col items-center justify-center gap-3 p-6 text-center'
  const sourceSwitcher = jsx(SourceSwitcher, {
    disabled: working,
    onChange: onSourceChange,
    source
  })
  if (status === 'listening') {
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', {
          className: 'animate-pulse text-2xl text-(--ui-accent)',
          children: '●'
        }),
        jsx('div', { className: 'font-medium', children: 'Microphone active' }),
        jsx('button', {
          type: 'button',
          disabled: working,
          onClick: stop,
          className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm hover:bg-(--chrome-action-hover)',
          children: 'Stop listening'
        })
      ]
    })
  }
  if (status === 'listening_unknown') {
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', {
          className: 'text-2xl text-(--ui-text-tertiary)',
          children: '◌'
        }),
        jsx('div', { className: 'font-medium', children: 'Microphone status unknown' }),
        jsx('p', {
          className: 'max-w-sm text-sm text-(--ui-text-tertiary)',
          children: 'Hermes could not refresh the listening state. Stop remains available until the backend confirms capture ended.'
        }),
        jsx('button', {
          type: 'button',
          disabled: working,
          onClick: stop,
          className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm hover:bg-(--chrome-action-hover)',
          children: 'Stop listening'
        })
      ]
    })
  }
  if (status === 'state_unknown') {
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', { className: 'text-2xl text-(--ui-text-tertiary)', children: '◌' }),
        jsx('div', { className: 'font-medium', children: 'Music status unknown' }),
        jsx('p', {
          className: 'max-w-sm text-sm text-(--ui-text-tertiary)',
          children: 'Hermes could not refresh the current backend state. Cached track and lyric details are hidden until polling recovers.'
        })
      ]
    })
  }
  if (['ambient_idle', 'no_match', 'recognition_error'].includes(status)) {
    const heading =
      status === 'no_match'
        ? 'No nearby song recognized'
        : status === 'recognition_error'
          ? 'Recognition unavailable'
          : 'Identify nearby music'
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', { className: 'text-3xl text-(--ui-text-quaternary)', children: '◉' }),
        jsx('div', { className: 'font-medium', children: heading }),
        jsx('p', {
          className: 'max-w-sm text-sm text-(--ui-text-tertiary)',
          children: "Experimental · Unofficial Shazam fingerprint service. An 8-second sample is fingerprinted on this Mac; the fingerprint, request metadata and your IP address reach Shazam. If a song matches, track metadata is sent to LRCLIB for lyrics. Showing recognized artwork contacts Apple's mzstatic.com CDN, which receives the image request and your IP address. Audio is not saved."
        }),
        jsx('button', {
          type: 'button',
          disabled: working,
          onClick: listen,
          className: cn(
            'rounded-md bg-(--ui-accent) px-3 py-1.5 text-sm',
            'disabled:pointer-events-none disabled:opacity-40'
          ),
          style: { color: 'var(--ui-bg-primary)' },
          children: 'Listen for 8 seconds'
        })
      ]
    })
  }
  if (status === 'permission_required') {
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', { className: 'text-2xl', children: '♫' }),
        jsx('div', { className: 'font-medium', children: 'Allow Music access' }),
        jsx('p', {
          className: 'max-w-sm text-sm text-(--ui-text-tertiary)',
          children: 'macOS needs one-time Automation permission so Hermes can read the current track and playback position.'
        }),
        jsx('button', {
          type: 'button',
          disabled: working,
          onClick: openPermissions,
          className: cn(
            'rounded-md bg-(--ui-accent) px-3 py-1.5 text-sm',
            'disabled:pointer-events-none disabled:opacity-40'
          ),
          style: { color: 'var(--ui-bg-primary)' },
          children: 'Open System Settings'
        })
      ]
    })
  }
  if (status === 'idle') {
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', { className: 'text-3xl text-(--ui-text-quaternary)', children: '♪' }),
        jsx('div', { className: 'font-medium', children: 'Play something in Apple Music' }),
        jsx('p', {
          className: 'text-sm text-(--ui-text-tertiary)',
          children: 'The lyrics pane will wake up automatically.'
        })
      ]
    })
  }
  if (status === 'not_found') {
    return jsxs('div', {
      className: common,
      children: [
        sourceSwitcher,
        jsx('div', { className: 'font-medium', children: 'No lyrics found' }),
        jsx('p', {
          className: 'max-w-sm text-sm text-(--ui-text-tertiary)',
          children:
            source === 'ambient'
              ? 'Song identified, but no synchronized lyrics were found. Try listening again.'
              : 'Try opening Lyrics once in Music.app so its local cache can populate, then refresh.'
        }),
        jsx('button', {
          type: 'button',
          disabled: working,
          onClick: source === 'ambient' ? listen : refresh,
          className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm hover:bg-(--chrome-action-hover)',
          children: source === 'ambient' ? 'Listen again' : 'Refresh lyrics'
        })
      ]
    })
  }
  return jsxs('div', {
    className: common,
    children: [
      sourceSwitcher,
      jsx('div', { className: 'font-medium', children: 'Lyrics backend unavailable' }),
      jsx('p', {
        className: 'text-sm text-(--ui-text-tertiary)',
        children: 'Enable the apple-music-lyrics backend and restart the local Hermes server.'
      })
    ]
  })
}

function MusicExperience({ ctx, compact = false }) {
  const profile = useValue(host.state.profile)
  const query = useMusicState(ctx, profile)
  const queryClient = useQueryClient()
  const retainedData = query.data
  const data = query.isError ? undefined : retainedData
  const ambientArtwork = data?.track?.source === 'ambient'
  const cachedArtworkUrl = useMemo(
    () => safeArtworkUrl(data?.artwork?.remote_url),
    [data?.artwork?.remote_url]
  )
  const artworkIdentity = data?.track?.identity || ''
  const artworkQuery = useMusicArtwork(
    ctx,
    profile,
    artworkIdentity,
    Boolean(data?.track?.title && data?.track?.source !== 'ambient')
  )
  const localArtworkUrl = useMemo(
    () => safeArtworkUrl(artworkQuery.data?.data_url),
    [artworkQuery.data?.data_url]
  )
  const artworkSources = useMemo(
    () =>
      ambientArtwork ? { url: cachedArtworkUrl, fallbackUrl: null } : selectArtworkSources(artworkQuery, cachedArtworkUrl, localArtworkUrl),
    [
      ambientArtwork,
      artworkQuery.data?.data_url,
      artworkQuery.error,
      artworkQuery.isError,
      artworkQuery.isSuccess,
      cachedArtworkUrl,
      localArtworkUrl
    ]
  )
  const artworkUrl = artworkSources.url
  const artworkFallbackUrl = artworkSources.fallbackUrl
  const position = useInterpolatedPosition(data?.track)
  const selectedSource = data?.track?.source === 'ambient' ? 'ambient' : 'music_app'
  const lastKnownSource =
    retainedData?.track?.source === 'ambient' ? 'ambient' : 'music_app'

  const command = useMutation({
    mutationFn: async input => {
      assertActiveProfile(profile)
      const result = await ctx.rest(input.path, {
        method: 'POST',
        body: input.body,
        timeoutMs: 7000
      })
      assertActiveProfile(profile)
      return result
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKey(ctx, profile) })
    },
    onError: error => {
      if (error?.message !== PROFILE_CHANGED_ERROR) {
        host.notifyError(error, 'Apple Music command failed')
      }
    }
  })

  const runAction = action => {
    haptic('tap')
    command.mutate({ path: '/control', body: { action } })
  }
  const runSeek = value => {
    command.mutate({ path: '/seek', body: { position: value } })
  }
  const refresh = () => {
    command.mutate(
      { path: '/refresh', body: {} },
      {
        onSuccess: () => {
          queryClient.removeQueries({
            queryKey: artworkProfileQueryKey(ctx, profile),
            predicate: candidate => candidate.queryKey.at(-1) !== artworkIdentity
          })
          void query.refetch()
          void artworkQuery.refetch()
        }
      }
    )
  }
  const openPermissions = () => {
    haptic('tap')
    command.mutate({ path: '/permissions', body: {} })
  }
  const selectSource = nextSource => {
    haptic('tap')
    command.mutate({ path: '/source', body: { source: nextSource } })
  }
  const listenAmbient = () => {
    haptic('tap')
    command.mutate({ path: '/ambient/listen', body: {} })
  }
  const stopAmbient = () => {
    haptic('tap')
    command.mutate({ path: '/ambient/stop', body: {} })
  }

  if (query.isLoading) {
    return jsxs('div', {
      className: 'flex h-full items-center justify-center gap-2 text-sm text-(--ui-text-tertiary)',
      children: [
        jsx('span', { className: 'animate-pulse', children: '♪' }),
        jsx('span', { children: 'Listening to Music.app…' })
      ]
    })
  }
  if (query.isError) {
    return jsx(StateNotice, {
      stop: stopAmbient,
      onSourceChange: selectSource,
      status:
        retainedData?.status === 'listening'
          ? 'listening_unknown'
          : retainedData
            ? 'state_unknown'
            : 'error',
      refresh,
      openPermissions,
      source: lastKnownSource,
      working: command.isPending
    })
  }
  if (
    !data ||
    [
      'idle',
      'permission_required',
      'error',
      'ambient_idle',
      'listening',
      'no_match',
      'recognition_error'
    ].includes(data.status)
  ) {
    return jsx(StateNotice, {
      listen: listenAmbient,
      onSourceChange: selectSource,
      status: data?.status || 'error',
      stop: stopAmbient,
      refresh,
      openPermissions,
      source: selectedSource,
      working: command.isPending
    })
  }

  return jsxs('div', {
    className: cn(
      'flex h-full min-h-0 flex-col overflow-hidden',
      compact ? 'text-sm' : 'mx-auto w-full'
    ),
    style: compact ? undefined : { maxWidth: '56rem' },
    children: [
      jsx(PlayerHeader, {
        artworkUrl,
        fallbackUrl: artworkFallbackUrl,
        compact,
        lyrics: data.lyrics,
        onSourceChange: selectSource,
        position,
        refresh,
        runAction,
        runSeek,
        source: selectedSource,
        track: data.track,
        working: command.isPending
      }),
      data.status === 'not_found'
        ? jsx(StateNotice, {
            listen: listenAmbient,
            onSourceChange: selectSource,
            status: 'not_found',
            refresh,
            openPermissions,
            source: selectedSource,
            working: command.isPending
          })
        : jsx(MemoizedLyricsScroller, {
            canSeek: data.track.can_seek !== false,
            compact,
            lyrics: data.lyrics,
            position: data.lyrics?.synced ? position : 0,
            seek: runSeek,
            working: command.isPending
          })
    ]
  })
}

function StatusChip({ ctx }) {
  const profile = useValue(host.state.profile)
  const query = useMusicState(ctx, profile)
  const retainedData = query.data
  const data = query.isError ? undefined : retainedData
  const position = useInterpolatedPosition(
    data?.track,
    Boolean(data?.lyrics?.synced)
  )
  const line = activeLineFor(data, position)
  const text =
    query.isError
      ? retainedData?.status === 'listening'
        ? 'Microphone status unknown'
        : 'Music status unknown'
      : data?.status === 'listening'
        ? 'Microphone active'
        : line?.text?.trim() || data?.track?.title || 'Apple Music'
  const label = text.length > 34 ? `${text.slice(0, 33)}…` : text

  return jsx('button', {
    type: 'button',
    title: 'Open Apple Music Lyrics',
    onClick: () => {
      haptic('tap')
      host.navigate(ROUTE)
    },
    className: cn(
      'inline-flex h-full max-w-64 items-center gap-1.5 px-1.5 text-[0.6875rem]',
      'text-(--ui-text-tertiary) transition-colors',
      'hover:bg-(--chrome-action-hover) hover:text-foreground'
    ),
    children: `♪ ${label}`
  })
}

export default {
  id: ID,
  name: 'Apple Music Lyrics',
  description: 'Synchronized lyrics for Music.app and manually recognized Nearby music',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Lyrics',
        data: {
          placement: 'right',
          dock: { pane: 'workspace', pos: 'right' },
          width: '360px'
        },
        render: () => jsx(MusicExperience, { ctx, compact: true })
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(MusicExperience, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Lyrics', codicon: 'music' }
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 115,
        render: () => jsx(StatusChip, { ctx })
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'apple-music-lyrics.open',
          label: 'Open Apple Music Lyrics',
          keywords: ['apple', 'music', 'lyrics', 'karaoke'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])
  }
}
