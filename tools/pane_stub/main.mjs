/**
 * Browser stand-in boot: fake ctx -> plugin register -> render status-bar chip.
 * The plugin exposes only register(ctx); the full WristPane opens through the
 * chip's "Open big" button, which calls host.openWorkspace (implemented in sdk-stub).
 * This is a labelled stand-in, not the real Hermes host.
 */

import { registerI18n } from '@hermes/plugin-sdk'
import { createRoot } from 'react-dom/client'
import { host } from '@hermes/plugin-sdk'
import plugin from '/plugin/hail_hermes/desktop/plugin.js'

const memoryStorage = new Map()

const NOW = Date.now() / 1000

const CANNED = {
  '/status': {
    running: true,
    devices: [],
    port: 8767,
    device_id: 'pane',
    mirror: {
      device: 'Galaxy Watch',
      screen: 'thinking',
      user: 'What is the weather?',
      reply: 'Sunny.',
      at: NOW
    }
  },
  '/settings': { lan_enabled: false, character: 'hermes' },
  // Models exist in the stub for the same reason the version chip does: a picker with no options
  // never exercises the search, the empty state, or the "saved but not live" row.
  '/models': [
    { url: 'http://127.0.0.1:11434', model: 'qwen3:8b', provider: 'ollama' }
  ],
  '/hermes-models': {
    provider: 'hermes',
    model: 'gpt-5.3-codex',
    providers: {
      hermes: ['gpt-5.3-codex', 'claude-sonnet-4.6', 'gemini-4-argon-high'],
      ollama: ['qwen3:8b', 'llama4:scout']
    }
  },
  '/soul-templates': {
    ok: true,
    templates: [
      { id: 'concise', name: 'Concise', description: 'Short, direct answers.', soul: '# Concise\nAnswer in one or two sentences.' },
      { id: 'storyteller', name: 'Storyteller', description: 'Warm, narrative replies.', soul: '# Storyteller\nReply like a friendly storyteller.' }
    ]
  },
  '/soul': { ok: true, soul: '# Hermes' },
  '/sessions': { sessions: [{ id: 's1', title: 'Morning chat', age: '2h' }] },
  '/setup-check': {
    ok: true,
    checks: [
      { id: 'ffmpeg', ok: false, message: 'Voice replies need a small free tool called ffmpeg.', fix: 'Install ffmpeg, then press Check again.' },
      { id: 'hermes', ok: true, message: 'Hermes found.', fix: '' }
    ]
  },
  '/face': { ok: true, data: null },
  '/pending': { pending: [] },
  '/models': [],
  '/memory': { lines: [] },
  '/identity': { device_id: 'pane' },
  // The version chip renders nothing when this route has no version, so a scan could not see
  // the undefined token it used to reference. Canned identity makes the chip actually render.
  '/v1/version': {
    version: '1.0.0-dev.184',
    release: '1.0.0',
    commit: '19f7a2c3d4e5f60718293a4b5c6d7e8f90123456',
    commit_date: '2026-11-09T19:00:00-07:00',
    built_at: '2026-11-09T19:04:11-07:00',
    dirty: false,
    channel: 'dev'
  }
}

const ctx = {
  i18n: {
    tables: {},
    register(table) {
      registerI18n(table)
      for (const [lang, entries] of Object.entries(table || {})) {
        const slot = ctx.i18n.tables[lang] || (ctx.i18n.tables[lang] = {})
        Object.assign(slot, entries)
      }
    }
  },
  onDispose(fn) {
    window.addEventListener('pagehide', () => { try { fn() } catch { /* ignore */ } })
  },
  registered: [],
  register(item) {
    ctx.registered.push(item)
  },
  storage: {
    get(key, fallback = null) {
      return memoryStorage.has(key) ? memoryStorage.get(key) : fallback
    },
    set(key, value) {
      memoryStorage.set(key, value)
    }
  },
  setInterval(fn, ms) {
    const id = window.setInterval(fn, ms)
    return () => window.clearInterval(id)
  },
  setTimeout(fn, ms) {
    const id = window.setTimeout(fn, ms)
    return () => window.clearTimeout(id)
  },
  async rest(path, _opts = {}) {
    void _opts
    const data = CANNED[path]
    return data !== undefined ? structuredClone(data) : { ok: true }
  }
}

plugin.default.register(ctx)

// Render the status-bar item (area 'right', render() -> React element).
const statusBar = document.getElementById('status-bar')
const entry = ctx.registered.find(item => item?.area === 'right' && typeof item?.render === 'function')
if (entry && statusBar) {
  createRoot(statusBar).render(entry.render())
} else {
  console.warn('[pane-stub] no status-bar item registered', ctx.registered)
}

// WristPane itself is not exported by the plugin; it renders when the chip's
// "Open big" is clicked -> openFullWristPane -> host.openWorkspace (sdk-stub).
// Keep a keybind-style hook so the alt+shift+h registration also works.
const toggle = ctx.registered.find(item => item?.area === 'keybinds' && typeof item?.data?.run === 'function')
window.addEventListener('keydown', ev => {
  if (ev.altKey && ev.shiftKey && (ev.key === 'H' || ev.key === 'h')) {
    ev.preventDefault()
    toggle?.data.run()
  }
})

console.info('[pane-stub] ready — click the "⌚ Hail Hermes" chip, then "Open big".')
void host
