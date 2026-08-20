import { createElement } from 'react'
import {
  useMutation,
  useQuery,
  useQueryClient
} from '@tanstack/react-query'

export const __sentinel = {}
globalThis.__appleMusicLyricsSdkSentinel = __sentinel

const profile = { get: () => 'default' }
export const host = {
  state: { profile },
  navigate() {},
  notify() {},
  notifyError() {}
}

export const PANES_AREA = 'panes'
export const PALETTE_AREA = 'commandPalette'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const STATUSBAR_AREAS = {
  left: 'statusBar.left',
  right: 'statusBar.right'
}
export const cn = (...values) => values.filter(Boolean).join(' ')
export const haptic = () => {}
export const SegmentedControl = props =>
  createElement('div', {
    'data-source': props.value,
    'data-testid': 'source-switcher'
  })
export { useMutation, useQuery, useQueryClient }
export const useValue = atom => atom.get()
