import assert from 'node:assert/strict'
import { realpathSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import {
  QueryClient,
  QueryClientProvider
} from '@tanstack/react-query'
import { __sentinel } from './desktop_plugin_sdk_shim.mjs'

assert.equal(globalThis.__lyricsForHermesSdkSentinel, __sentinel)

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/'
})
Object.defineProperties(globalThis, {
  document: { configurable: true, value: dom.window.document },
  HTMLElement: { configurable: true, value: dom.window.HTMLElement },
  MutationObserver: { configurable: true, value: dom.window.MutationObserver },
  navigator: { configurable: true, value: dom.window.navigator },
  Node: { configurable: true, value: dom.window.Node },
  window: { configurable: true, value: dom.window }
})
globalThis.IS_REACT_ACT_ENVIRONMENT = true
window.matchMedia = () => ({
  addEventListener() {},
  matches: false,
  removeEventListener() {}
})
window.requestAnimationFrame = callback => window.setTimeout(callback, 0)
window.cancelAnimationFrame = handle => window.clearTimeout(handle)
HTMLElement.prototype.scrollIntoView = () => {}

const pluginUrl = pathToFileURL(realpathSync(process.env.PLUGIN_PATH)).href
const plugin = (await import(pluginUrl)).default
assert.equal(plugin.id, 'lyrics-for-hermes')

let currentState = null
let failStatePoll = false
const contributions = []
const context = {
  source: 'plugin:lyrics-for-hermes',
  async rest(path) {
    if (path !== '/state') throw new Error(`unexpected REST path: ${path}`)
    if (failStatePoll) throw new Error('503: state polling unavailable')
    return currentState
  },
  registerMany(items) {
    contributions.push(...items)
    return () => {}
  },
  i18n: {},
  os: {},
  storage: {}
}
plugin.register(context)
const page = contributions.find(item => item.id === 'page')
const status = contributions.find(item => item.id === 'status')
assert.equal(typeof page?.render, 'function')
assert.equal(typeof status?.render, 'function')

const stateQueryKey = [
  'lyrics-for-hermes',
  context.source,
  'default',
  'state'
]
const waitFor = async (predicate, message) => {
  const deadline = Date.now() + 2000
  while (Date.now() < deadline) {
    if (predicate()) return
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 10))
    })
  }
  assert.fail(message)
}
const occurrences = (text, needle) => text.split(needle).length - 1

async function runRetainedFailureScenario({
  failureText,
  initialState,
  initialText,
  verifyFailure
}) {
  currentState = initialState
  failStatePoll = false
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { gcTime: Infinity }
    }
  })
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)

  const App = () =>
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(
        React.Fragment,
        null,
        page.render(),
        status.render()
      )
    )

  await act(async () => {
    root.render(React.createElement(App))
  })
  await waitFor(
    () => container.textContent.includes(initialText),
    `initial state did not render: ${initialText}`
  )

  failStatePoll = true
  await act(async () => {
    await queryClient.refetchQueries({ exact: true, queryKey: stateQueryKey })
  })
  await waitFor(
    () => queryClient.getQueryState(stateQueryKey)?.status === 'error',
    'refetch failure did not retain an error state'
  )
  assert.deepEqual(queryClient.getQueryData(stateQueryKey), initialState)
  await waitFor(
    () => container.textContent.includes(failureText),
    `failed state did not render ${failureText}: ${container.textContent}`
  )
  verifyFailure(container.textContent)

  await act(async () => {
    root.unmount()
  })
  queryClient.clear()
  container.remove()
}

const readyState = {
  status: 'ready',
  track: {
    album: 'stale album',
    artist: 'stale artist',
    can_control: false,
    can_seek: false,
    duration: 200,
    identity: 'a'.repeat(24),
    position: 10,
    running: true,
    sampled_at: Date.now() / 1000,
    source: 'music_app',
    state: 'playing',
    title: 'stale title'
  },
  lyrics: {
    lines: [{ text: 'stale lyric', time: 0 }],
    source: 'LRCLIB',
    synced: true
  },
  artwork: { remote_url: null }
}
await runRetainedFailureScenario({
  failureText: 'Music status unknown',
  initialState: readyState,
  initialText: 'stale title',
  verifyFailure(text) {
    assert.ok(occurrences(text, 'Music status unknown') >= 2, text)
    assert.doesNotMatch(text, /stale title|stale lyric|stale artist|stale album/)
  }
})

dom.window.close()
console.log(JSON.stringify({ passed: true, scenarios: 1 }))
