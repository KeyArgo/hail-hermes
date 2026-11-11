import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const { LINK, OUT_DIR = '.' } = process.env
if (!LINK) throw new Error('LINK is required')
mkdirSync(OUT_DIR, { recursive: true })
const browser = await chromium.launch({ headless: true, args: [
  '--mute-audio', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'
] })
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
  page.on('pageerror', e => console.error('PAGEERROR', e.message))
  page.on('console', m => { if (m.type() === 'error') console.error('CONSOLE', m.text()) })
  page.on('requestfailed', r => console.error('REQFAILED', r.url()))
  await page.addInitScript(() => { localStorage.setItem('hermes.desktop.pluginDecisions.v2', JSON.stringify({ 'hail-hermes': true })) })
  await page.goto(LINK, { waitUntil: 'domcontentloaded' })
  const chip = page.getByRole('button', { name: /⌚ Hail Hermes/ }).first()
  try {
    await chip.waitFor({ state: 'visible', timeout: 30000 })
  } catch (e) {
    await page.screenshot({ path: resolve(OUT_DIR, 'no-chip.png') })
    throw e
  }
  await chip.screenshot({ path: resolve(OUT_DIR, 'chip.png') })
  await chip.click()
  const popover = page.getByRole('dialog').filter({ hasText: 'Open big' }).first()
  await popover.waitFor({ state: 'visible' })
  await popover.screenshot({ path: resolve(OUT_DIR, 'popover.png') })
  await popover.getByRole('button', { name: /Open big/ }).click()
  await page.getByText('Quick ask', { exact: false }).first().waitFor({ state: 'visible' })
  await page.screenshot({ path: resolve(OUT_DIR, 'big-tab.png') })
  for (const label of ['Talk', 'Character', 'Sessions', 'Watch', 'Settings']) {
    await page.getByRole('button', { name: label, exact: true }).first().click()
    await page.waitForTimeout(300)
    await page.screenshot({ path: resolve(OUT_DIR, `tab-${label.toLowerCase()}.png`) })
  }
  await page.getByRole('button', { name: 'Talk', exact: true }).first().click()
  if (await page.getByRole('alert').filter({ hasText: 'Timed out' }).count()) throw new Error('Timed out alert')
} finally {
  await browser.close()
}
