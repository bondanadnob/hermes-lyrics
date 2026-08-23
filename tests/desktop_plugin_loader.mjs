import { realpathSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const repositoryRoot = realpathSync(process.env.HERMES_REPO_ROOT)
const pluginUrl = pathToFileURL(realpathSync(process.env.PLUGIN_PATH)).href
const shimUrl = pathToFileURL(realpathSync(process.env.SDK_SHIM_PATH)).href
const moduleUrl = relativePath =>
  pathToFileURL(realpathSync(path.join(repositoryRoot, 'node_modules', relativePath))).href

const mappedSpecifiers = new Map([
  ['@hermes/plugin-sdk', shimUrl],
  ['@tanstack/react-query', moduleUrl('@tanstack/react-query/build/modern/index.js')],
  ['jsdom', moduleUrl('jsdom/lib/api.js')],
  ['react', moduleUrl('react/index.js')],
  ['react-dom/client', moduleUrl('react-dom/client.js')],
  ['react/jsx-runtime', moduleUrl('react/jsx-runtime.js')]
])

export async function resolve(specifier, context, nextResolve) {
  const mapped = mappedSpecifiers.get(specifier)
  if (mapped) return { url: mapped, shortCircuit: true }
  return nextResolve(specifier, context)
}

export async function load(url, context, nextLoad) {
  if (url === pluginUrl) {
    return {
      format: 'module',
      source: readFileSync(new URL(url), 'utf8'),
      shortCircuit: true
    }
  }
  return nextLoad(url, context)
}
