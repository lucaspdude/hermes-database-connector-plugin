// @hermes/plugin-sdk stub: records queries/invalidations/navigations and
// renders kit components as tagged nodes so tests can walk the tree.
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
