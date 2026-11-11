import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

const pluginSrc = readFileSync(new URL('../hail_hermes/desktop/plugin.js', import.meta.url), 'utf8')

const HELPER_START = '/** Pure audio + transcript helpers'
const HELPER_END = '\nconst ID = '

async function loadAudioHelpers() {
  const start = pluginSrc.indexOf(HELPER_START)
  const end = pluginSrc.indexOf(HELPER_END)
  assert.ok(start >= 0 && end > start, 'helper block not found in plugin.js')
  const code = pluginSrc.slice(start, end)
  const url = `data:text/javascript;charset=utf-8,${encodeURIComponent(code)}`
  return import(url)
}

test('plugin.js only imports allowed modules', () => {
  const imports = [...pluginSrc.matchAll(/^import[\s\S]*?from\s+['"]([^'"]+)['"]/gm)]
  for (const [, spec] of imports) {
    const allowed = spec === '@hermes/plugin-sdk'
      || spec === 'react'
      || spec === 'react/jsx-runtime'
    assert.ok(allowed, `disallowed import from ${spec}`)
  }
})

test('plugin.js has no relative imports', () => {
  const imports = [...pluginSrc.matchAll(/^import[\s\S]*?from\s+['"]([^'"]+)['"]/gm)]
  for (const [, spec] of imports) {
    assert.ok(!spec.startsWith('.'), `relative import from ${spec}`)
  }
})

test('plugin.js avoids forbidden patterns', () => {
  assert.doesNotMatch(pluginSrc, /\beval\s*\(/)
  assert.doesNotMatch(pluginSrc, /\bnew\s+Function\s*\(/)
  assert.doesNotMatch(pluginSrc, /\bdocument\.querySelector/)
  assert.doesNotMatch(pluginSrc, /\binnerHTML\b/)
})

test('plugin.js loads sample through ctx.rest bridge', () => {
  assert.match(pluginSrc, /ctx\.rest\(['"]\/sample['"]/)
  assert.match(pluginSrc, /fetch\('http:\/\/127\.0\.0\.1:8767\/status'/)
})

test('plugin.js binds usePluginI18n to plugin id', () => {
  assert.match(pluginSrc, /const t = usePluginI18n\(ID\)/)
})

test('plugin.js uses function i18n entries for interpolated copy', () => {
  assert.match(pluginSrc, /codeLabel:\s*\(\s*vars\s*\)\s*=>/)
  assert.match(pluginSrc, /screen:\s*\(\s*vars\s*\)\s*=>/)
})

test('plugin.js stops reconnect after hub denies pairing', () => {
  assert.match(pluginSrc, /msg\.code\s*===\s*['"]denied['"]/)
  assert.match(pluginSrc, /wantConnectionRef\.current\s*=\s*false/)
})

test('plugin.js gates session frames on welcome connected state', () => {
  assert.match(pluginSrc, /function sendText\(\)[\s\S]*?if\s*\(\s*!connected\s*\)/)
  assert.match(pluginSrc, /function interrupt\(\)[\s\S]*?if\s*\(\s*!connected\s*\)/)
  assert.match(pluginSrc, /async function sendPcmUtterance[\s\S]*?if\s*\(\s*!connected\s*\)/)
})

test('plugin.js reconnect uses latest hub status', () => {
  assert.match(pluginSrc, /hubRef\.current = status/)
  assert.match(pluginSrc, /void refreshHub\(\)\.then/)
})

test('plugin.js keeps reconnecting while hub stays down', () => {
  assert.match(pluginSrc, /function scheduleReconnect/)
  assert.match(pluginSrc, /scheduleReconnect\(\)/)
})

test('plugin.js jsx calls put text in props.children (not the third argument)', () => {
  const bad = [...pluginSrc.matchAll(
    /\bjsx\s*\(\s*[\w$.'"]+\s*,\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*,\s*(?!['"])/g
  )]
  assert.equal(bad.length, 0, `jsx() must not pass children as third argument: ${bad.length} hit(s)`)
})

test('plugin.js polls hub with ctx.setInterval disposer on unmount', () => {
  assert.match(pluginSrc, /const stopPoll = ctx\.setInterval/)
  assert.match(pluginSrc, /return stopPoll/)
  assert.doesNotMatch(pluginSrc, /ctx\.clearInterval/)
})

test('plugin.js keeps idle socket alive and reconnects', () => {
  assert.match(pluginSrc, /type:\s*'ping'/)
  assert.match(pluginSrc, /ctx\.setTimeout/)
  assert.match(pluginSrc, /notifyRestError/)
})

test('plugin.js uses protocol v2 and never pair_confirm', () => {
  assert.match(pluginSrc, /v:\s*2/)
  assert.doesNotMatch(pluginSrc, /pair_confirm/)
})

test('plugin.js status chip uses StatusDot tone', () => {
  assert.match(pluginSrc, /pending\.length \|\| inlineNotice \? 'warn'/)
  assert.match(pluginSrc, /whiteSpace: 'nowrap'/)
  assert.match(pluginSrc, /Approve \$\{pending\[0\]\.match_code/)
})

test('plugin.js streams mic PCM during capture', () => {
  assert.match(pluginSrc, /pushPcmAndDrainFrames/)
  assert.match(pluginSrc, /streamUtteranceRef/)
  assert.match(pluginSrc, /captureActiveRef/)
  assert.match(pluginSrc, /captureActiveRef\.current = true/)
})

test('plugin.js sends audio_start only after getUserMedia on live socket', () => {
  assert.match(
    pluginSrc,
    /getUserMedia[\s\S]*?const activeWs = wsRef\.current[\s\S]*?activeWs\.send\(pocketMsg\('audio_start'/
  )
})

test('plugin.js hides mic only for permission errors', () => {
  assert.match(pluginSrc, /function micPermissionLost/)
  assert.match(pluginSrc, /micPermissionLost\(err\)/)
})

test('plugin.js always shows text input and sample button', () => {
  assert.match(pluginSrc, /placeholder:\s*t\('textPlaceholder'\)/)
  assert.match(pluginSrc, /children:\s*t\('sendSample'\)/)
  // The mic row is only a problem message; the text box and sample button must not be inside it.
  const micRow = pluginSrc.slice(pluginSrc.indexOf('!micOk'), pluginSrc.indexOf("t('micUnavailable')") + 200)
  assert.doesNotMatch(micRow, /sendText|sendSample/)
})

test('plugin.js discards capture on socket close and stop', () => {
  assert.match(pluginSrc, /function cancelCaptureDiscard/)
  assert.match(pluginSrc, /ws\.onclose[\s\S]*cancelCaptureDiscard/)
  assert.match(pluginSrc, /function interrupt\(\)[\s\S]*cancelCaptureDiscard/)
})

test('plugin.js serializes sample and mic outbound audio', () => {
  assert.match(pluginSrc, /async function lockUtterance/)
  assert.match(pluginSrc, /async function sendSample[\s\S]*lockUtterance/)
  assert.match(pluginSrc, /async function startTalk[\s\S]*lockUtterance/)
  assert.match(pluginSrc, /shouldDeferOutboundAudio/)
})

test('plugin.js hello uses device_id from hub status', () => {
  assert.match(pluginSrc, /device_id:\s*latest\.device_id/)
  assert.doesNotMatch(pluginSrc, /function deviceIdFor/)
  assert.doesNotMatch(pluginSrc, /STORAGE_DEVICE/)
})

test('plugin.js auto-approves desktop pairing via approve-self', () => {
  assert.match(pluginSrc, /ctx\.rest\(['"]\/approve-self['"]/)
  assert.match(
    pluginSrc,
    /type === 'pair_pending'[\s\S]*ctx\.rest\(['"]\/approve-self['"]/
  )
  const pendingHandler = pluginSrc.slice(pluginSrc.indexOf("type === 'pair_pending'"), pluginSrc.indexOf("type === 'paired'"))
  assert.doesNotMatch(pendingHandler, /setMatchCode/)
})

test('plugin.js filters its own device from pending list', () => {
  assert.match(pluginSrc, /rows\.filter\(row => row\.device_id !== selfId\)/)
})

test('plugin.js connects automatically when hub is running', () => {
  assert.match(
    pluginSrc,
    /useEffect\(\(\) => \{[\s\S]*hub\?\.running[\s\S]*openSocketRef\.current\(\)/
  )
  assert.doesNotMatch(pluginSrc, /children:\s*t\('connect'\)/)
})

test('plugin.js keeps Approve controls for other pending devices', () => {
  assert.match(pluginSrc, /children:\s*t\('approve'\)/)
  assert.match(pluginSrc, /ctx\.rest\(['"]\/approve['"]/)
})

test('status polling uses bounded bridge requests', () => {
  assert.match(pluginSrc, /ctx\.rest\('\/status', \{ timeoutMs: 3000 \}\)/)
})

test('plugin.js surfaces hub startup failure with Retry', () => {
  assert.match(pluginSrc, /t\('retryHub'\)/)
  assert.match(pluginSrc, /!hub\?\.running && hubError/)
})

test('plugin.js surfaces stt_failed with typed fallback copy', () => {
  assert.match(pluginSrc, /msg\.code === 'stt_failed'/)
  assert.match(pluginSrc, /sttNotSetup/)
})

test('plugin.js infers text-only replies without audio_start', () => {
  assert.match(pluginSrc, /turnExpectsAudioRef/)
  assert.match(pluginSrc, /turnGotAudioRef/)
  assert.match(pluginSrc, /ttsTextOnly/)
})

test('downsampleTo16k halves length from 48 kHz', async () => {
  const { downsampleTo16k } = await loadAudioHelpers()
  const input = new Float32Array(480)
  for (let i = 0; i < input.length; i += 1) {
    input[i] = Math.sin(i / 10)
  }
  const out = downsampleTo16k(input, 48000)
  assert.equal(out.length, 160)
})

test('floatToS16le produces little-endian bytes', async () => {
  const { floatToS16le } = await loadAudioHelpers()
  const bytes = floatToS16le(new Float32Array([0, 1, -1]))
  assert.equal(bytes.length, 6)
  const view = new DataView(bytes.buffer)
  assert.equal(view.getInt16(2, true), 32767)
})

test('stripWavHeader returns pcm chunk', async () => {
  const { stripWavHeader } = await loadAudioHelpers()
  const header = new Uint8Array(44)
  header.set([0x52, 0x49, 0x46, 0x46]) // RIFF
  const data = new Uint8Array([...header, 0x01, 0x00, 0x02, 0x00])
  const pcm = stripWavHeader(data)
  assert.ok(pcm.length >= 4)
})

test('chunkPcm16 emits 640-byte frames', async () => {
  const { POCKET_FRAME_BYTES, chunkPcm16 } = await loadAudioHelpers()
  const pcm = new Uint8Array(POCKET_FRAME_BYTES * 2 + 100)
  const frames = chunkPcm16(pcm)
  assert.equal(frames.length, 3)
  assert.equal(frames[0].length, POCKET_FRAME_BYTES)
  assert.equal(frames[1].length, POCKET_FRAME_BYTES)
})

test('pushPcmAndDrainFrames yields full frames from partial input', async () => {
  const { POCKET_FRAME_BYTES, pushPcmAndDrainFrames } = await loadAudioHelpers()
  let remainder = new Uint8Array(0)
  const chunk = new Uint8Array(POCKET_FRAME_BYTES + 10)
  const { frames, remainder: rest } = pushPcmAndDrainFrames(remainder, chunk)
  assert.equal(frames.length, 1)
  assert.equal(frames[0].length, POCKET_FRAME_BYTES)
  assert.equal(rest.length, 10)
})

test('reduceTranscript appends deltas then replaces on final', async () => {
  const { reduceTranscript, transcriptDisplayLines } = await loadAudioHelpers()
  let lines = []
  lines = reduceTranscript(lines, { role: 'assistant', text: 'Hello ', final: false })
  lines = reduceTranscript(lines, { role: 'assistant', text: 'world', final: false })
  lines = reduceTranscript(lines, { role: 'assistant', text: 'Hello world', final: true })
  assert.deepEqual(transcriptDisplayLines(lines), ['assistant: Hello world'])
})

test('reduceTranscript matches protocol transcript-final fixture', async () => {
  const { reduceTranscript, transcriptDisplayLines } = await loadAudioHelpers()
  const finalMsg = JSON.parse(readFileSync(
    new URL('./fixtures/protocol/transcript-final.json', import.meta.url),
    'utf8'
  ))
  let lines = reduceTranscript([], { role: 'assistant', text: 'Hello ', final: false })
  lines = reduceTranscript(lines, { role: 'assistant', text: 'world', final: false })
  lines = reduceTranscript(lines, finalMsg)
  assert.deepEqual(transcriptDisplayLines(lines), [`${finalMsg.role}: ${finalMsg.text}`])
})

test('closeOpenTranscriptRows prevents stale assistant draft merge', async () => {
  const { closeOpenTranscriptRows, reduceTranscript, transcriptDisplayLines } = await loadAudioHelpers()
  let lines = reduceTranscript([], { role: 'assistant', text: 'partial', final: false })
  lines = closeOpenTranscriptRows(lines)
  lines = reduceTranscript(lines, { role: 'assistant', text: 'fresh', final: true })
  assert.deepEqual(transcriptDisplayLines(lines), ['assistant: partial', 'assistant: fresh'])
})

test('shouldDeferOutboundAudio blocks during capture', async () => {
  const { shouldDeferOutboundAudio } = await loadAudioHelpers()
  assert.equal(shouldDeferOutboundAudio({ captureActive: false, holdTalk: false, streamUtterance: false, talking: false, talkPending: false }), false)
  assert.equal(shouldDeferOutboundAudio({ captureActive: true, holdTalk: false, streamUtterance: false, talking: false, talkPending: false }), true)
  assert.equal(shouldDeferOutboundAudio({ captureActive: false, holdTalk: true, streamUtterance: false, talking: false, talkPending: false }), true)
})

test('pocketMsg helper sends v2 audio bracket', () => {
  assert.match(pluginSrc, /pocketMsg\('audio_start'/)
  assert.match(pluginSrc, /rate:\s*POCKET_SAMPLE_RATE/)
  assert.match(pluginSrc, /pocketMsg\('audio_end'/)
})

test('plugin.js registers status bar Popover in STATUSBAR_AREAS.right', () => {
  assert.match(pluginSrc, /area:\s*STATUSBAR_AREAS\.right/)
  assert.match(pluginSrc, /⌚ Hail Hermes/)
  assert.match(pluginSrc, /\bPopover\b/)
  assert.match(pluginSrc, /\bPopoverTrigger\b/)
  assert.match(pluginSrc, /\bPopoverContent\b/)
  assert.match(pluginSrc, /width:\s*'320px'/)
  assert.match(pluginSrc, /Open big/)
  assert.match(pluginSrc, /Paired devices/)
  assert.match(pluginSrc, /Quick ask/)
})

test('plugin.js polls /status every 5s with exponential backoff, visibility check, and no overlap', async () => {
  const { POLL_BASE_MS, POLL_BACKOFF_MS, nextPollDelayMs, isPaneVisible } = await loadAudioHelpers()
  assert.equal(POLL_BASE_MS, 5000)
  assert.deepEqual(POLL_BACKOFF_MS, [5000, 10000, 20000, 30000])
  assert.equal(nextPollDelayMs(0), 5000)
  assert.equal(nextPollDelayMs(1), 10000)
  assert.equal(nextPollDelayMs(2), 20000)
  assert.equal(nextPollDelayMs(3), 30000)
  assert.equal(nextPollDelayMs(8), 30000)
  assert.equal(isPaneVisible(null, { hidden: true, visibilityState: 'hidden' }), false)
  assert.equal(isPaneVisible(null, { hidden: false, visibilityState: 'visible' }), true)
  assert.match(pluginSrc, /pollInFlightRef\.current/)
  assert.match(pluginSrc, /isPaneVisible\(paneRootRef\.current\)/)
  assert.match(pluginSrc, /mountedRef\.current\s*=\s*false/)
})

test('plugin.js buttons await routes, disable while running, and show inline feedback', () => {
  assert.match(pluginSrc, /ctx\.rest\(['"]\/start['"]/)
  assert.match(pluginSrc, /Hub started/)
  assert.match(pluginSrc, /Approved/)
  assert.match(pluginSrc, /disabled:\s*!!busyAction/)
  assert.match(pluginSrc, /disabled:\s*!!busyKey/)
})


test('panel uses a closeable workspace and a rebindable popover shortcut', () => {
  assert.doesNotMatch(pluginSrc, /PANES_AREA|openPane/)
  assert.match(pluginSrc, /'aria-label': 'Close'/)
  assert.match(pluginSrc, /onOpenChange: setOpen/)
  assert.match(pluginSrc, /typeof host\.openWorkspace !== 'function'/)
  assert.match(pluginSrc, /host\.openWorkspace\('hail-hermes'/)
  assert.match(pluginSrc, /onClose: \(\) => \{ closeWorkspace = null \}/)
  assert.match(pluginSrc, /ctx\.onDispose\(/)
  assert.match(pluginSrc, /area: KEYBINDS_AREA/)
  assert.match(pluginSrc, /defaults: \['alt\+shift\+h'\]/)
  assert.match(pluginSrc, /ctx\.storage\.get\('last-view'/)
  assert.match(pluginSrc, /ctx\.storage\.set\('last-view'/)
  assert.match(pluginSrc, /Hermes is busy — retrying…/)
  assert.match(pluginSrc, /No microphone found/)
  assert.match(pluginSrc, /Microphone is blocked — allow it in system settings/)
})

test('direct polling is shared and does not depend on the bridge', async () => {
  const start = pluginSrc.indexOf('// One polling cycle is shared')
  const end = pluginSrc.indexOf('\nlet closeWorkspace', start)
  const helpers = await loadAudioHelpers()
  const source = `const POLL_BASE_MS = 5000; const nextPollDelayMs = ${helpers.nextPollDelayMs.toString()}; const POLL_BACKOFF_MS = [5000, 10000, 20000, 30000]; let wristSnapshot = {};\n${pluginSrc.slice(start, end)}\nexport { pollStatus };`
  const { pollStatus } = await import(`data:text/javascript,${encodeURIComponent(source)}`)
  const oldFetch = globalThis.fetch
  let release, calls = 0
  globalThis.fetch = async (url, options) => {
    calls++
    assert.equal(url, 'http://127.0.0.1:8767/status')
    assert.ok(options.signal)
    await new Promise(resolve => { release = resolve })
    return { ok: true, json: async () => ({ running: true, devices: [], pending: [{ name: 'Watch', match_code: '4821' }] }) }
  }
  const ctx = { rest: async route => { assert.equal(route, '/settings'); throw Error('overloaded') } }
  try {
    const first = pollStatus(ctx), second = pollStatus(ctx)
    assert.equal(calls, 1)
    release()
    const [result] = await Promise.all([first, second])
    assert.equal(result.pend.pending[0].match_code, '4821')
    assert.equal(result.status.running, true)
    await pollStatus(ctx)
    assert.equal(calls, 1)
  } finally { globalThis.fetch = oldFetch }
})

test('watch pairing is visible from the chip and settings', () => {
  assert.match(pluginSrc, /`Approve \$\{pending\[0\]\.match_code\}`/)
  assert.match(pluginSrc, /Your watch wants to connect — does it show \$\{row\.match_code\}\?/)
  assert.match(pluginSrc, /handleDecision\('approve', row\.request_id\)/)
  assert.match(pluginSrc, /handleDecision\('deny', row\.request_id\)/)
  assert.match(pluginSrc, /Let my watch connect/)
  assert.match(pluginSrc, /savePatch\(\{ lan_enabled: ev\.target\.checked \}\)/)
  assert.match(pluginSrc, /Forget Watch/)
})

test('wrist pane tab bar orders five tabs with EN copy', () => {
  const tabRowStart = pluginSrc.indexOf("role: 'tablist'")
  const tabRowEnd = pluginSrc.indexOf('showSettings', tabRowStart)
  const tabRow = pluginSrc.slice(tabRowStart, tabRowEnd)
  assert.ok(tabRow.includes('tablist'), 'tab row not found')
  let pos = -1
  for (const id of ['talk', 'character', 'sessions', 'watch', 'settings']) {
    const keyPos = tabRow.indexOf(`'${id}'`)
    assert.ok(keyPos > pos, `tab ${id} out of order`)
    pos = keyPos
  }
  assert.match(pluginSrc, /role: 'tablist'/)
  for (const [key, value] of [
    ['tab_talk', 'Talk'],
    ['tab_character', 'Character'],
    ['tab_sessions', 'Sessions'],
    ['tab_watch', 'Watch'],
    ['tab_settings', 'Settings'],
    ['soulSaved', 'Personality saved.'],
    ['soulUse', 'Use this personality'],
    ['soulLoadFailed', 'Could not load the personalities. Try again.']
  ]) {
    assert.ok(
      new RegExp(`${key}:\\s*'`).test(pluginSrc) && pluginSrc.includes(`${key}: '${value}'`),
      `missing EN key ${key}`
    )
  }
})

test('character tab renders templates from the soul-templates route', () => {
  assert.match(pluginSrc, /function CharacterTab\(\{ ctx \}\)/)
  assert.match(pluginSrc, /ctx\.rest\('\/soul-templates', \{ timeoutMs: 4000 \}\)/)
  assert.match(pluginSrc, /jsx\(CharacterTab, \{ ctx \}\)/)
  assert.doesNotMatch(pluginSrc, /tabCharacterSoon/)
})

test('character tab uploads and previews the face via /face', () => {
  const tabStart = pluginSrc.indexOf('function CharacterTab')
  const tabEnd = pluginSrc.indexOf('\nlet closeWorkspace', tabStart)
  const tab = pluginSrc.slice(tabStart, tabEnd)
  assert.match(pluginSrc, /ctx\.rest\('\/face', \{ timeoutMs: 4000 \}\)/)
  assert.match(tab, /ctx\.rest\('\/face', \{\s*method: 'POST'/)
  assert.match(tab, /ctx\.rest\('\/face\/reset', \{ method: 'POST', timeoutMs: 4000 \}\)/)
  assert.match(tab, /accept: 'image\/png,image\/jpeg'/)
  assert.match(tab, /file\.size > 2 \* 1024 \* 1024/)
  assert.match(tab, /t\('faceTooBig'\)/)
  assert.match(tab, /t\('faceReset'\)/)
  assert.match(tab, /t\('faceSaved'\)/)
  assert.match(tab, /jsx\('img', \{/)
})

test('sessions tab lists conversations from the sessions route', () => {
  assert.match(pluginSrc, /function SessionsTab\(\{ ctx \}\)/)
  assert.match(pluginSrc, /ctx\.rest\('\/sessions', \{ timeoutMs: 4000 \}\)/)
  assert.match(pluginSrc, /jsx\(SessionsTab, \{ ctx \}\)/)
  assert.doesNotMatch(pluginSrc, /tabSessionsSoon/)
  for (const key of ['sessionsEmpty', 'sessionsLoadFailed', 'sessionsRetry']) {
    assert.match(pluginSrc, new RegExp(`${key}:\\s*'`), `missing EN key ${key}`)
  }
})

test('sessions tab opens a session through the guarded host bridge', () => {
  assert.match(pluginSrc, /await host\.openSession\(id\)/)
  assert.match(pluginSrc, /typeof host\?\.openSession !== 'function'/)
  const openStart = pluginSrc.indexOf('async function openSessionRow')
  assert.ok(openStart > 0, 'openSessionRow helper not found')
  const openEnd = pluginSrc.indexOf('function CharacterTab', openStart)
  const helper = pluginSrc.slice(openStart, openEnd)
    + pluginSrc.slice(pluginSrc.indexOf('function SessionsTab'), pluginSrc.indexOf('function CharacterTab'))
  assert.match(pluginSrc, /children:\s*t\('sessionsOpen'\)/)
  assert.match(helper, /host\.notify\(\{\s*kind:\s*'warning',\s*message:\s*t\('sessionsOpenFailed'\)\s*\}\)/)
  assert.match(pluginSrc, /sessionsOpen: 'Open in Hermes'/)
  assert.match(pluginSrc, /sessionsOpenFailed: 'Could not open that conversation\.'/)
  // Rows without an id must not render the button.
  const rowsStart = pluginSrc.indexOf('.map(({ item }) => {', pluginSrc.indexOf('function SessionsTab'))
  const rows = pluginSrc.slice(rowsStart, pluginSrc.indexOf('function CharacterTab', rowsStart))
  assert.match(rows, /typeof item\?\.id === 'string' && item\.id\s*\?[\s\S]*?t\('sessionsOpen'\)/)
})

test('watch tab has its own switch posting lan_enabled', () => {
  assert.match(pluginSrc, /function WatchTab\(\{ ctx \}\)/)
  const beforeWrist = pluginSrc.slice(0, pluginSrc.indexOf('function WristPane'))
  assert.match(beforeWrist, /function WatchTab/)
  const tabStart = pluginSrc.indexOf('function WatchTab')
  const tabEnd = pluginSrc.indexOf('function WristPane', tabStart)
  const tab = pluginSrc.slice(tabStart, tabEnd)
  assert.match(tab, /ctx\.rest\('\/settings', \{ timeoutMs: 4000 \}\)/)
  assert.match(tab, /ctx\.rest\('\/settings', \{\s*method: 'POST',\s*body: \{ lan_enabled: next \},\s*timeoutMs: 4000\s*\}\)/)
  assert.match(tab, /role: 'switch'/)
  assert.match(tab, /t\('watchConnect'\)/)
  assert.match(tab, /t\('watchConnectFailed'\)/)
  assert.match(tab, /t\('watchConnectHelp'\)/)
  assert.match(pluginSrc, /watchConnect: 'Let my watch connect'/)
  assert.match(pluginSrc, /watchConnectFailed: 'That did not work\. Please try again\.'/)
  assert.match(pluginSrc, /watchConnectHelp: 'Your watch and this computer must be on the same Wi-Fi\.'/)
  assert.match(pluginSrc, /jsx\(WatchTab, \{ ctx \}\)/)
  assert.doesNotMatch(pluginSrc, /tabWatchPairing/)
})

test('mirror card polls status and clears its timer', () => {
  assert.match(pluginSrc, /function MirrorCard\(\{ ctx \}\)/)
  assert.match(pluginSrc, /jsx\(MirrorCard, \{ ctx \}\)/)
  const cardStart = pluginSrc.indexOf('function MirrorCard')
  const cardEnd = pluginSrc.indexOf('function WristPane', cardStart)
  const card = pluginSrc.slice(cardStart, cardEnd)
  assert.match(card, /ctx\.rest\('\/status', \{ timeoutMs: 1500 \}\)/)
  assert.match(card, /ctx\.setInterval\(/)
  assert.match(card, /alive = false/)
  assert.match(card, /if \(timer\) timer\(\)/)
  const beforeWrist = pluginSrc.slice(0, pluginSrc.indexOf('function WristPane'))
  assert.match(beforeWrist, /function MirrorCard/)
  assert.match(pluginSrc, /t\('mirrorNone'\)/)
  assert.match(pluginSrc, /t\('mirrorStale'\)/)
  assert.match(pluginSrc, /t\('mirror_' \+ \(mirror\.screen \|\| 'idle'\)\)/)
  assert.match(pluginSrc, /mirrorTitle: 'On your watch'/)
  assert.match(pluginSrc, /mirrorNone: 'No watch connected yet\.'/)
  assert.match(pluginSrc, /mirrorStale: 'Last seen a while ago\.'/)
  assert.match(pluginSrc, /mirror_idle: 'Waiting'/)
  assert.match(pluginSrc, /mirror_listening: 'Listening'/)
  assert.match(pluginSrc, /mirror_thinking: 'Thinking'/)
  assert.match(pluginSrc, /mirror_speaking: 'Speaking'/)
})

test('setup card runs the setup-check route in settings', () => {
  assert.match(pluginSrc, /function SetupCard\(\{ ctx \}\)/)
  assert.match(pluginSrc, /ctx\.rest\('\/setup-check', \{ timeoutMs: 4000 \}\)/)
  assert.match(pluginSrc, /jsx\(SetupCard, \{ ctx \}\)/)
  for (const key of ['setupTitle', 'setupCheckAgain', 'setupLoadFailed']) {
    assert.match(pluginSrc, new RegExp(`${key}:\\s*'`), `missing EN key ${key}`)
  }
})

test('plugin.js parses as a module', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pane-parse-'))
  try {
    const copy = join(dir, 'plugin.mjs')
    writeFileSync(copy, pluginSrc)
    execFileSync(process.execPath, ['--check', copy], { stdio: 'pipe' })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})


test('a failed mic start releases the talk lock once, not twice', () => {
  // cancelCaptureDiscard() already unlocks; a second unlock in finally hands the lock to a second waiter.
  const start = pluginSrc.indexOf('async function startTalk')
  const end = pluginSrc.indexOf('async function endTalk', start)
  const body = pluginSrc.slice(start, end)
  const finallyPart = body.slice(body.lastIndexOf('} finally {'))
  assert.match(body, /catch \(err\) \{\s*holdsLock = false\s*cancelCaptureDiscard\(\)/)
  assert.match(finallyPart, /if \(holdsLock &&/)
})


test('endTalk after an interrupt does not release a lock it no longer owns', () => {
  const start = pluginSrc.indexOf('async function endTalk')
  const end = pluginSrc.indexOf('async function sendSample', start)
  const body = pluginSrc.slice(start, end)
  assert.match(body, /const ownsLock = captureActiveRef\.current \|\| streamUtteranceRef\.current/)
  assert.match(body, /if \(ownsLock\) \{\s*unlockUtterance\(\)/)
})


test('the version chip uses a colour token that is actually bound', () => {
  // The chip used `text.tertiary`, but the module only declares textPrimary/textSecondary —
  // a ReferenceError at render time, on the one widget that must always work. `node --check`
  // cannot see it, so guard the whole module against the bare `text.` namespace.
  assert.doesNotMatch(pluginSrc, /\btext\.(primary|secondary|tertiary)\b/)
  const start = pluginSrc.indexOf('function HailVersionChip')
  const end = pluginSrc.indexOf('function WristStatusChip', start)
  const body = pluginSrc.slice(start, end)
  assert.match(body, /color: build\.dirty \? '#b45309' : textSecondary/)
})

