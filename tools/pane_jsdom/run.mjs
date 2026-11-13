// Runs the real plugin.js in jsdom with the stand-in SDK: no browser, no Hermes.
// Proves rendering and clicks, not real-host loading. Setup: see README.md.
// Usage, from a folder holding the npm packages: node run.mjs <repo root>
import { JSDOM } from 'jsdom'
import { cpSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = process.argv[2] || join(here, '..', '..')
// `--fresh` renders the pane the way a brand-new profile looks: nothing paired, no models, no facts, no
// sessions. A pane that only ever sees a populated profile is a pane whose empty states are untested.
const FRESH = process.argv.includes('--fresh')
const sdk = join(process.cwd(), 'node_modules', '@hermes', 'plugin-sdk')
mkdirSync(sdk, { recursive: true })
cpSync(join(repo, 'tools/pane_stub/sdk-stub.mjs'), join(sdk, 'index.mjs'))
writeFileSync(join(sdk, 'package.json'), '{"name":"@hermes/plugin-sdk","type":"module","exports":"./index.mjs"}')
const pluginCopy = join(process.cwd(), 'plugin.mjs')
writeFileSync(pluginCopy, readFileSync(join(repo, 'plugin/hail_hermes/desktop/plugin.js')))

const dom = new JSDOM('<!doctype html><body><div id="statusbar"></div><div id="workspace"></div></body>', { url: 'http://127.0.0.1/', pretendToBeVisual: true })
const g = globalThis
for (const k of ['window', 'document', 'HTMLElement', 'Node', 'Element', 'Event', 'KeyboardEvent', 'MouseEvent', 'localStorage', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame', 'MutationObserver']) g[k] = dom.window[k]
Object.defineProperty(g, 'navigator', { value: dom.window.navigator, configurable: true })
const errors = []
console.error = (...a) => errors.push(a.map(String).join(' ').slice(0, 240))
process.on('uncaughtException', e => errors.push('UNCAUGHT ' + String(e.stack || e).slice(0, 300)))
process.on('unhandledRejection', e => errors.push('REJECT ' + String(e?.stack || e).slice(0, 300)))
g.WebSocket = class { constructor() { setTimeout(() => this.onerror?.({}), 5) } close() {} send() {} }
g.AudioContext = dom.window.AudioContext = class { close() {} }
g.fetch = async () => { throw new Error('no network in harness') }

const main = readFileSync(join(repo, 'tools/pane_stub/main.mjs'), 'utf8')
const canned = main.slice(main.indexOf('const CANNED'), main.indexOf('const ctx'))
const CANNED = new Function('const NOW = Date.now() / 1000;' + canned + '\nreturn CANNED')()
if (FRESH) {
  CANNED['/models'] = []
  CANNED['/hermes-models'] = { providers: [], models: [] }
  CANNED['/memory'] = { ok: true, lines: [] }
  CANNED['/sessions'] = { sessions: [] }
  CANNED['/slots'] = { slots: [] }
  CANNED['/pending'] = { pending: [] }
  CANNED['/settings'] = { ...(CANNED['/settings'] || {}), lan_enabled: false, model: '', provider: '' }
}
const { default: plugin } = await import(pathToFileURL(pluginCopy).href)
const { createRoot } = await import('react-dom/client')
const { createElement } = await import('react')

// A bundle that cannot be imported is captured by the error handlers above and used to end the whole
// run silently, with exit 0 and no output — the harness "passed" while nothing had been loaded. A
// load failure is the one bug this file exists to catch, so it fails here, visibly.
if (errors.length) {
  console.log('FAIL bundle did not load')
  for (const e of [...new Set(errors)].slice(0, 4)) console.log('  ', e)
  process.exit(1)
}
const stub = await import('@hermes/plugin-sdk')

const calls = []
const opened = []
const mem = new Map()
stub.host.openSession = async id => { opened.push(id) }
const ctx = {
  i18n: { register: t => stub.registerI18n(t) },
  onDispose() {},
  registered: [],
  register(i) { ctx.registered.push(i) },
  storage: { get: (k, f = null) => (mem.has(k) ? mem.get(k) : f), set: (k, v) => mem.set(k, v) },
  setInterval: (f, ms) => { const i = setInterval(f, ms); return () => clearInterval(i) },
  setTimeout: (f, ms) => { const i = setTimeout(f, ms); return () => clearTimeout(i) },
  async rest(p, o = {}) {
    calls.push([p, JSON.stringify(o.body || null)])
    const d = CANNED[p.split('?')[0]]
    return d !== undefined ? structuredClone(d) : { ok: true }
  }
}
plugin.register(ctx)

const wait = ms => new Promise(r => setTimeout(r, ms))
const waitFor = async (fn, ms = 2500) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await wait(50) } return false }
const fails = []
const check = (name, ok) => { console.log((ok ? 'PASS ' : 'FAIL ') + name); if (!ok) fails.push(name) }
const ws = () => document.getElementById('workspace').textContent
const button = name => [...document.querySelectorAll('button')].find(b => b.textContent.includes(name))
const tabs = () => [...document.querySelectorAll('[role=tablist] button')]
const go = async name => { tabs().find(b => b.textContent === name)?.click(); await wait(350) }

const chipItem = ctx.registered.find(i => i.id === 'wrist-status')
createRoot(document.getElementById('statusbar')).render(createElement(() => chipItem.render()))
// The build stamp is NOT a status-bar item any more: it lives in the pane. A second copy in the
// status bar was clutter, so the contract is that only the ⌚ chip is registered.
check('build stamp is not a status-bar item', !ctx.registered.some(i => i.id === 'version'))
const navItem = ctx.registered.find(i => i.id === 'nav')
check('the left rail carries a Hail Hermes entry',
  !!navItem && navItem.area === 'sidebarNav' && navItem.data?.path === '/hail' && navItem.data?.label === 'Hail Hermes')
const pageItem = ctx.registered.find(i => i.id === 'page')
check('the rail entry opens a real page',
  !!pageItem && pageItem.area === 'routes' && pageItem.data?.path === '/hail' && typeof pageItem.render === 'function')
await wait(300)
check('chip shows', document.getElementById('statusbar').textContent.includes('Hail Hermes'))
button('Hail Hermes')?.click()
await wait(300)
check('chip click opens popover', !!button('Open big'))
button('Open big')?.click()
await wait(500)
check('four tabs, panel first', tabs().map(b => b.textContent).join() === 'Talk,Sessions,Character,Settings')
check('watch is not a peer tab', !tabs().some(b => b.textContent === 'Watch'))
check('header says who answers', ws().includes('Ready') || ws().includes('Hub not running'))
await go('Talk')
if (FRESH) {
  // A fresh profile: no watch, no brief, no answer — and nothing that pretends otherwise.
  check('Talk does not claim a connected watch', !ws().includes('WATCH CONNECTED'))
  check('Talk offers a way to pair', /pair|connect/i.test(ws()))
  check('Talk has no crash text', !/not defined|undefined|NaN/.test(ws()))
} else {
  check('Talk shows watch mirror', ws().includes('Galaxy Watch') && ws().includes('Sunny.'))
}
check('Talk header carries the build stamp', /v\d+\.\d+\.\d+/.test(ws()))
// One chip, drawn as a pill — the version alone. The old monospace "v… · date · sha" line read as
// debug output, and Settings printed a second, different version of the same build.
const stampBtn = [...document.querySelectorAll('button')].find(b => /^v\d+\.\d+\.\d+/.test(b.textContent.trim()))
// The house format: version · date · short commit, all three visible in one pill.
check('the stamp carries version, date and commit',
  !!stampBtn && /^v\d+\.\d+\.\d+\S* · \d{4}-\d{2}-\d{2} · [0-9a-f]{8}$/.test(stampBtn.textContent.trim()))
// A machine with authorised models must never be told it has none. The stub serves three Hermes
// providers, so this is the exact falsehood a first-run judge saw.
if (!FRESH) {
  // On a machine with models this must never be claimed. On a fresh profile it is the honest answer.
  check('never claims "no model" when Hermes has models', !ws().includes('No model yet'))
}
await go('Character')
check('Character lists personalities', ws().includes('Concise'))
check('Character has no crash text', !/not defined|undefined/.test(ws()))
button('Use this personality')?.click()
await wait(200)
check('Use this personality saves', calls.some(c => c[0] === '/soul' && c[1] !== 'null'))
check('Character confirms save', ws().includes('Personality saved.'))
const voices = [...document.querySelectorAll('input[name=pocket-character-tab]')]
check('Character has the voice picker', voices.length >= 2)
const avatarImgs = [...document.querySelectorAll('img[src^="data:image/png"]')]
check('every look has a face', avatarImgs.length >= 8)
check('the faces are different pictures', new Set(avatarImgs.map(a => a.src)).size >= 8)
voices[1]?.click()
await wait(200)
check('Voice picker saves', calls.some(c => c[0] === '/settings' && c[1].includes('character')))
await go('Sessions')
if (FRESH) {
  check('an empty profile says there are no conversations',
    ws().includes('No conversations yet') && !/undefined/.test(ws()))
} else {
  check('Sessions lists a chat', ws().includes('Morning chat'))
  button('Open in Hermes')?.click()
  await wait(100)
  check('Open in Hermes calls host', opened[0] === 's1')
}
button('More')?.click()
await wait(200)
button('Watch')?.click()
await wait(300)
const sw = document.querySelector('[role=switch]')
check('Watch has the switch', !!sw)
sw?.click()
await wait(200)
check('Watch switch saves', calls.some(c => c[0] === '/settings' && c[1].includes('lan_enabled')))
await go('Settings')
check('Settings shows setup card', ws().includes('Voice replies need'))
check('Settings keeps old settings', ws().includes('1. Character'))
// The model list is a search problem: typing must filter it, and an empty result must say so.
const modelSearch = document.querySelector('input[type=search]')
check('Settings has a model search', !!modelSearch)
// Facts are the plugin's own list for quick ask, not Hermes' MEMORY.md. Calling them "Memory
// (MEMORY.md)" told people their additions reached the agent's memory, which they never did.
check('the facts section is named for what it is', ws().includes('Facts for quick answers'))
check('the facts section no longer claims to be MEMORY.md', !ws().includes('Memory (MEMORY.md)'))
check('the section says which mode reads it', /quick ask/i.test(ws()) && /session/i.test(ws()))
check(FRESH ? 'an empty facts list says so' : 'the pane shows the facts it has',
  FRESH ? /No facts yet/i.test(ws()) : ws().includes('The studio is upstairs'))
const factInput = [...document.querySelectorAll('input[type=text]')]
  .find(i => /Add a fact/i.test(i.placeholder || ''))
check('the pane offers to add a fact', !!factInput)
if (factInput) {
  // React tracks the DOM value itself, so a plain assignment is invisible to it; set through the
  // prototype setter the way a real keystroke would.
  const setValue = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
  setValue.call(factInput, 'I like espresso')
  factInput.dispatchEvent(new Event('input', { bubbles: true }))
  await wait(150)
  button('Add fact')?.click()
  await wait(250)
  check('adding a fact goes to the plugin route', calls.some(c => c[0] === '/memory' && c[1].includes('espresso')))
  if (!calls.some(c => c[0] === '/memory' && c[1].includes('espresso'))) {
    console.log('   DEBUG calls:', JSON.stringify(calls.filter(c => String(c[0]).includes('mem'))))
    console.log('   DEBUG input value:', factInput.value)
  }
}
if (FRESH) {
  check('Brain says plainly that there is no model yet',
    /no model/i.test(ws()) && !/undefined/.test(ws()))
} else {
  check('Brain lists models the hub knows about', ws().includes('gpt-5.3-codex') || ws().includes('qwen3:8b'))
}
if (modelSearch) {
  modelSearch.value = 'zz-no-such-model'
  modelSearch.dispatchEvent(new Event('input', { bubbles: true }))
  await wait(250)
  check('model search filters to nothing and says so', /No model matches|No models found/.test(ws()))
  modelSearch.value = ''
  modelSearch.dispatchEvent(new Event('input', { bubbles: true }))
  await wait(250)
  if (!FRESH) {
    check('clearing the search restores the list', !/No model matches that|No models found/.test(ws()))
  }
}
// The routed page is a second mount of the same component, so it is rendered last: earlier checks
// must not accidentally click the page copy instead of the pane copy.
if (pageItem) {
  const pageHost = document.createElement('div')
  document.body.appendChild(pageHost)
  createRoot(pageHost).render(createElement(() => pageItem.render()))
  await wait(600)
  check('the page renders the panel, not an empty route',
    pageHost.textContent.includes('Hail Hermes') && pageHost.textContent.includes('Talk'))
}

const real = errors.filter(e => !/key|ARIA|ariaLabel/i.test(e))
check('no runtime errors', real.length === 0)
for (const e of [...new Set(real)].slice(0, 8)) console.log('  ', e)
console.log(`${fails.length} failed, ${errors.length - real.length} warnings`)
process.exit(fails.length ? 1 : 0)
