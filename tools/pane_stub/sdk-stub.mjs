/**
 * Stand-in of @hermes/plugin-sdk for browser preview only. Not the real host.
 * Matched to how plugin/hail_hermes/desktop/plugin.js consumes the SDK.
 */

import { cloneElement, createElement, isValidElement } from 'react'
import { createRoot } from 'react-dom/client'

const i18nTables = { en: {} }

export function registerI18n(tables) {
  Object.assign(i18nTables.en, tables?.en || {})
}

export function Button({ size, variant, onClick, disabled, children, ...rest }) {
  const fontSize = size === 'sm' ? '11px' : '13px'
  const styles = {
    padding: variant === 'ghost' ? '2px 6px' : '5px 12px',
    borderRadius: '8px',
    border: variant === 'primary' ? '1px solid #4c8bd8' : '1px solid #3a4149',
    background: variant === 'primary' ? '#2b5f9e' : variant === 'secondary' ? '#232830' : 'transparent',
    color: '#e7e9ee',
    fontSize,
    cursor: disabled ? 'default' : 'pointer',
    opacity: disabled ? 0.6 : 1
  }
  return createElement('button', { type: 'button', onClick, disabled, style: styles, ...rest }, children)
}

export function EmptyState({ title }) {
  return createElement(
    'div',
    { style: { color: '#9aa3ad', border: '1px dashed #3a4149', borderRadius: '8px', padding: '12px' } },
    title
  )
}

export function Popover({ open, onOpenChange, children, ...rest }) {
  void rest
  const [trigger, content] = Array.isArray(children) ? children : [children]
  const isOpen = !!open
  return createElement(
    'div',
    { style: { position: 'relative', display: 'inline-block' },
      onKeyDown: ev => { if (ev.key === 'Escape') onOpenChange?.(false) } },
    // Clone the trigger to add click-to-toggle (plugin passes a plain button via asChild).
    isValidElement(trigger)
      ? createElement(trigger.type, {
        ...trigger.props,
        onClick: ev => {
          trigger.props?.onClick?.(ev)
          onOpenChange?.(!isOpen)
        }
      })
      : trigger,
    isOpen ? content : null
  )
}

export function PopoverContent({ align, side, style, children, ...rest }) {
  void align
  void side
  return createElement(
    'div',
    {
      role: 'dialog',
      style: {
        position: 'absolute',
        bottom: 'calc(100% + 6px)',
        right: 0,
        zIndex: 50,
        background: '#1d2126',
        border: '1px solid #3a4149',
        borderRadius: '10px',
        boxShadow: '0 8px 24px rgba(0,0,0,0.4)',
        ...style
      },
      ...rest
    },
    children
  )
}

export function PopoverTrigger({ asChild, children, ...rest }) {
  // With asChild the child (a plain button element) is rendered as-is; the Popover
  // wrapper above attaches the toggle. Without asChild, render a plain button.
  if (asChild && isValidElement(children)) {
    return cloneElement(children, rest)
  }
  return createElement('button', { type: 'button', ...rest }, children)
}

export function StatusDot({ tone }) {
  const colors = { good: '#3fae6a', warn: '#d8a13a', bad: '#d05a4e', muted: '#6b7280' }
  return createElement('span', {
    'data-tone': tone,
    style: {
      display: 'inline-block',
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      background: colors[tone] || colors.muted
    }
  })
}

export const KEYBINDS_AREA = 'keybinds'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebarNav'
export const PANES_AREA = 'panes'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'left', right: 'right' }

export function usePluginI18n(id) {
  void id
  return (key, params) => {
    const entry = i18nTables.en?.[key]
    if (typeof entry === 'function') {
      return entry(params || {})
    }
    if (typeof entry === 'string') {
      return entry
    }
    return key
  }
}

export const host = {
  notify(msg) {
    console.log('[host.notify]', msg)
  },
  async openSession(id) {
    console.log('[host.openSession]', id)
  },
  openWorkspace(id, opts = {}) {
    console.log('[host.openWorkspace]', id, opts?.title || '')
    const mount = document.getElementById('workspace')
    if (!mount || typeof opts.render !== 'function') {
      return () => {}
    }
    const container = document.createElement('section')
    container.dataset.paneWorkspace = id
    if (opts.title) {
      const heading = document.createElement('strong')
      heading.textContent = opts.title
      container.appendChild(heading)
    }
    const body = document.createElement('div')
    container.appendChild(body)
    mount.replaceChildren(container)
    const root = createRoot(body)
    root.render(opts.render())
    const close = () => {
      root.unmount()
      container.remove()
    }
    if (typeof opts.onClose === 'function') {
      const onClose = opts.onClose
      return () => {
        onClose()
        close()
      }
    }
    return close
  }
}

export default host
