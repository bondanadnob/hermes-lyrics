import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'

let source = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
source = source
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*'@hermes\/plugin-sdk'\s*/, '')
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*'react'\s*/, '')
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*'react\/jsx-runtime'\s*/, '')
  .replace('export default {', 'globalThis.__plugin = {')
source += '\nglobalThis.__artworkHooks = { artworkRetry, MusicExperience, safeArtworkUrl, selectArtworkSources, StatusChip, useMusicArtwork, useMusicState };\n'

const sandbox = {
  URL,
  PALETTE_AREA: 'palette',
  PANES_AREA: 'panes',
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar',
  STATUSBAR_AREAS: { right: 'status-right' },
  cn: (...values) => values.filter(Boolean).join(' '),
  haptic: () => {},
  host: {
    state: { profile: { get: () => 'default' } },
    navigate: () => {},
    notifyError: () => {}
  },
  useMutation: () => ({ isPending: false, mutate: () => {} }),
  useQuery: options => options,
  useQueryClient: () => ({ invalidateQueries: () => {}, removeQueries: () => {} }),
  useValue: atom => atom.get(),
  memo: component => component,
  useEffect: () => {},
  useMemo: factory => factory(),
  useRef: value => ({ current: value }),
  useState: value => [value, () => {}],
  jsx: (type, props) => ({ type, props }),
  jsxs: (type, props) => ({ type, props }),
  window: {
    clearInterval: () => {},
    matchMedia: () => ({ matches: false }),
    setInterval: () => 1,
    setTimeout
  },
  console,
  encodeURIComponent,
  Error,
  RegExp
}
vm.createContext(sandbox)
vm.runInContext(source, sandbox, { filename: 'desktop/plugin.js' })

const {
  artworkRetry,
  MusicExperience,
  safeArtworkUrl,
  selectArtworkSources,
  StatusChip,
  useMusicArtwork,
  useMusicState
} = sandbox.__artworkHooks
const identity = 'a'.repeat(24)
const local = 'data:image/jpeg;base64,/9j/'
const remote = 'https://is1-ssl.mzstatic.com/320x320.jpg'
const plain = value => JSON.parse(JSON.stringify(value))

assert.equal(useMusicState({ source: 'local', rest: async () => ({}) }, 'default').retry, false)

assert.equal(safeArtworkUrl('data:image/jpeg;base64,AAA'), null)
assert.equal(safeArtworkUrl('data:image/jpeg;base64,/9j='), null)
assert.equal(safeArtworkUrl(local), local)

assert.deepEqual(
  plain(selectArtworkSources({ isSuccess: false, isError: false }, remote)),
  { url: null, fallbackUrl: null }
)
assert.deepEqual(
  plain(selectArtworkSources(
    { isSuccess: true, isError: false, data: { data_url: local } },
    remote
  )),
  { url: local, fallbackUrl: remote }
)
assert.deepEqual(
  plain(selectArtworkSources({ isSuccess: false, isError: true }, remote)),
  { url: remote, fallbackUrl: null }
)
assert.deepEqual(
  plain(selectArtworkSources(
    {
      data: { identity, data_url: local },
      isSuccess: false,
      isError: true,
      error: new Error('404: absent')
    },
    remote
  )),
  { url: remote, fallbackUrl: null }
)
assert.deepEqual(
  plain(selectArtworkSources(
    {
      data: { identity, data_url: local },
      isSuccess: false,
      isError: true,
      error: new Error('404: absent')
    },
    `https://is1-ssl.mzstatic.com/${'x'.repeat(2050)}`
  )),
  { url: null, fallbackUrl: null }
)
assert.deepEqual(
  plain(selectArtworkSources(
    { isSuccess: false, isError: true, error: new Error('409: stale') },
    remote
  )),
  { url: null, fallbackUrl: null }
)

assert.equal(artworkRetry(0, new Error('503: busy')), true)
assert.equal(artworkRetry(1, new Error('503: transient')), true)
assert.equal(artworkRetry(2, new Error('503: still unavailable')), false)
assert.equal(artworkRetry(0, new Error('404: absent')), false)
assert.equal(artworkRetry(0, new Error('409: stale')), false)
assert.equal(artworkRetry(0, new Error('Active Hermes profile changed during Lyrics for Hermes request')), false)

const wrapped503 = new Error("Error invoking remote method 'hermes:api': Error: 503: busy")
const wrapped409 = new Error(
  "Error invoking remote method 'hermes:api': Error: 409: stale identity"
)
assert.equal(artworkRetry(0, wrapped503), true)
assert.equal(artworkRetry(2, wrapped503), false)
assert.deepEqual(
  plain(selectArtworkSources({ isSuccess: false, isError: true, error: wrapped409 }, remote)),
  { url: null, fallbackUrl: null }
)

const validQuery = useMusicArtwork(
  { source: 'local', rest: async () => ({ artwork: { identity, data_url: local } }) },
  'default',
  identity,
  true
)
assert.equal(validQuery.retry, artworkRetry)
assert.equal(validQuery.retryOnMount, false)
assert.equal(validQuery.refetchOnMount, false)
assert.equal(validQuery.refetchOnWindowFocus, false)
assert.equal(validQuery.refetchOnReconnect, false)
assert.equal((await validQuery.queryFn()).data_url, local)

const malformedQuery = useMusicArtwork(
  { source: 'local', rest: async () => ({ artwork: null }) },
  'default',
  identity,
  true
)
await assert.rejects(malformedQuery.queryFn(), /invalid artwork response/i)

let refreshRemoveFilter = null
let currentArtworkRefetches = 0
const refreshState = {
  status: 'ready',
  track: {
    running: true,
    state: 'paused',
    title: 'Synthetic',
    artist: 'Artist',
    album: 'Album',
    duration: 200,
    position: 1,
    sampled_at: Date.now() / 1000,
    identity
  },
  lyrics: { source: 'LRCLIB', synced: true, lines: [] },
  artwork: { remote_url: remote }
}
sandbox.useValue = () => 'default'
sandbox.useQuery = options =>
  options.queryKey.at(-1) === 'state'
    ? { data: refreshState, isLoading: false, isError: false, refetch: async () => {} }
    : {
        data: { identity, data_url: local },
        isSuccess: true,
        isError: false,
        refetch: async () => { currentArtworkRefetches += 1 }
      }
sandbox.useQueryClient = () => ({
  invalidateQueries: () => {},
  removeQueries: filter => { refreshRemoveFilter = filter }
})
sandbox.useMutation = options => ({
  isPending: false,
  mutate: (_input, callbacks = {}) => {
    options.onSuccess?.()
    callbacks.onSuccess?.()
  }
})
const refreshTree = MusicExperience({
  ctx: { source: 'local', rest: async () => ({}) },
  compact: false
})

const findNode = (value, predicate) => {
  if (!value || typeof value !== 'object') return null
  if (predicate(value)) return value
  const children = value.props?.children
  for (const child of Array.isArray(children) ? children : [children]) {
    const match = findNode(child, predicate)
    if (match) return match
  }
  return null
}
const refreshHeader = findNode(
  refreshTree,
  node => typeof node.props?.refresh === 'function' && node.props?.track?.identity === identity
)
assert.ok(refreshHeader)
refreshHeader.props.refresh()
assert.equal(typeof refreshRemoveFilter?.predicate, 'function')
assert.equal(
  refreshRemoveFilter.predicate({ queryKey: ['lyrics-for-hermes', 'local', 'default', 'artwork', identity] }),
  false
)
assert.equal(
  refreshRemoveFilter.predicate({ queryKey: ['lyrics-for-hermes', 'local', 'default', 'artwork', 'b'.repeat(24)] }),
  true
)
assert.equal(currentArtworkRefetches, 1)

const staleState = {
  status: 'ready',
  track: {
    running: true,
    state: 'playing',
    title: 'stale title',
    artist: 'stale artist',
    album: 'stale album',
    duration: 200,
    position: 10,
    sampled_at: Date.now() / 1000,
    identity
  },
  lyrics: {
    source: 'LRCLIB',
    synced: true,
    lines: [{ time: 0, text: 'stale lyric' }]
  },
  artwork: { remote_url: remote }
}

const memoSlots = []
let memoCursor = 0
sandbox.useMemo = (factory, dependencies) => {
  const index = memoCursor++
  const previous = memoSlots[index]
  if (
    previous &&
    previous.dependencies.length === dependencies.length &&
    previous.dependencies.every((value, dependencyIndex) =>
      Object.is(value, dependencies[dependencyIndex])
    )
  ) {
    return previous.value
  }
  const value = factory()
  memoSlots[index] = { dependencies, value }
  return value
}
let retainedArtworkQuery = {
  data: { identity, data_url: local },
  isSuccess: false,
  isError: true,
  error: wrapped503,
  refetch: async () => {}
}
sandbox.useValue = () => 'default'
sandbox.useQuery = options =>
  options.queryKey.at(-1) === 'state'
    ? { data: staleState, isLoading: false, isError: false, refetch: async () => {} }
    : retainedArtworkQuery
sandbox.useQueryClient = () => ({ invalidateQueries: () => {}, removeQueries: () => {} })
sandbox.useMutation = () => ({ mutate: () => {}, isPending: false })

memoCursor = 0
const transientArtworkTree = MusicExperience({
  ctx: { source: 'local', rest: async () => ({}) },
  compact: false
})
assert.match(JSON.stringify(transientArtworkTree), /data:image\/jpeg|mzstatic/)

retainedArtworkQuery = { ...retainedArtworkQuery, error: wrapped409 }
memoCursor = 0
const staleArtworkTree = MusicExperience({
  ctx: { source: 'local', rest: async () => ({}) },
  compact: false
})
assert.doesNotMatch(JSON.stringify(staleArtworkTree), /data:image\/jpeg|mzstatic/)

sandbox.useMemo = factory => factory()
sandbox.useValue = () => 'default'
let staleArtworkEnabled = null
sandbox.useQueryClient = () => ({ invalidateQueries: () => {}, removeQueries: () => {} })
sandbox.useMutation = () => ({ mutate: () => {}, isPending: false })
sandbox.jsx = (type, props) =>
  typeof type === 'function' ? type(props || {}) : { type, props: props || {} }
sandbox.jsxs = sandbox.jsx
sandbox.useQuery = options => {
  if (options.queryKey.at(-1) === 'state') {
    return { data: staleState, isLoading: false, isError: true, refetch: async () => {} }
  }
  staleArtworkEnabled = options.enabled
  return {
    data: undefined,
    isSuccess: false,
    isError: false,
    refetch: async () => {}
  }
}
const retainedReadyTree = MusicExperience({
  ctx: { source: 'local', rest: async () => ({}) },
  compact: false
})
const retainedReadyText = JSON.stringify(retainedReadyTree)
assert.match(retainedReadyText, /Music status unknown/)
assert.doesNotMatch(retainedReadyText, /stale title|stale lyric/)
assert.equal(staleArtworkEnabled, false)
const retainedReadyStatusText = JSON.stringify(StatusChip({
  ctx: { source: 'local', rest: async () => ({}) }
}))
assert.match(retainedReadyStatusText, /Music status unknown/)
assert.doesNotMatch(retainedReadyStatusText, /stale title|stale lyric/)

console.log(JSON.stringify({ passed: true }))
