// @hermes/plugin-sdk stub: records queries/invalidations/navigations, renders
// kit components as tagged nodes and resolves plugin i18n bundles like the
// app's plugin-i18n (active locale → en → raw key).
const component = (name) => (props) => ({ sdk: name, props: props ?? {} })

export const Badge = component('Badge')
export const Button = component('Button')
export const ConfirmDialog = component('ConfirmDialog')
export const CopyButton = component('CopyButton')
export const EmptyState = component('EmptyState')
export const ErrorState = component('ErrorState')
export const Input = component('Input')
export const LogView = component('LogView')
export const Select = component('Select')
export const SelectContent = component('SelectContent')
export const SelectItem = component('SelectItem')
export const SelectTrigger = component('SelectTrigger')
export const SelectValue = component('SelectValue')
export const StatusDot = component('StatusDot')
export const Switch = component('Switch')

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusbar.left', right: 'statusbar.right' }

export const host = {
  navigations: [],
  navigate(path) {
    host.navigations.push(path)
  },
  notify() {},
}

export const queryClient = {
  invalidations: [],
  invalidateQueries(opts) {
    queryClient.invalidations.push(opts)
  },
}

let stubData = {}
let stubIsError = false
export const queryCalls = []

export function setStubData(data, { isError = false } = {}) {
  stubData = data
  stubIsError = isError
}

export function useQuery(opts) {
  queryCalls.push(opts)
  return { data: stubData, isError: stubIsError, isLoading: false, error: null }
}

export function resetQueryCalls() {
  queryCalls.length = 0
  queryClient.invalidations.length = 0
  host.navigations.length = 0
}

export const pluginSettingsHref = (id, page) =>
  '/settings?tab=plugins&plugin=' + id + '&ppage=' + page

export const relativeTime = () => 'agora'

// ---- plugin i18n (mirrors src/i18n/plugin-i18n.ts resolution) ----

const i18nRegistry = new Map() // plugin id → { locale → messages }
export let activeLocale = 'en'
export function setStubLocale(locale) {
  activeLocale = locale
}

function resolvePath(messages, key) {
  let node = messages
  for (const part of key.split('.')) {
    if (node === null || typeof node !== 'object') return undefined
    node = node[part]
  }
  return node
}

function translatePlugin(pluginId, key, args) {
  const byLocale = i18nRegistry.get(pluginId)
  for (const locale of [activeLocale, 'en']) {
    const value = byLocale && resolvePath(byLocale[locale], key)
    if (value !== undefined) {
      return typeof value === 'function' ? value(...args) : String(value)
    }
  }
  return key
}

export function usePluginI18n(pluginId) {
  return (key, ...args) => translatePlugin(pluginId, key, args)
}

export function registerStubLocales(pluginId, bundles) {
  const prev = i18nRegistry.get(pluginId) ?? {}
  i18nRegistry.set(pluginId, { ...prev, ...bundles })
}