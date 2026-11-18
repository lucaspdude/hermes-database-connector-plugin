// database-connector — desktop half (Hermes Desktop Plugin SDK).
// ESM on disk, loaded uncompiled: no JSX syntax, no build step, no hex colors,
// no window timers. Children always go through props (jsx/jsxs contract) and
// every element inside an array child carries a key (no React warnings).
// State: useQuery + shared queryClient; events only invalidate — REST
// (refetch + 5 s polling) is the source of truth.
// i18n: plugin-scoped locale bundles via ctx.i18n.register + usePluginI18n
// (same pattern as the bundled kanban plugin); components re-render on locale
// switch, static surfaces (nav/palette) rebuild via ctx.i18n.onLocaleChange.
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useMemo, useRef, useState } from 'react'
import {
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  PALETTE_AREA,
  STATUSBAR_AREAS,
  Badge,
  Button,
  ConfirmDialog,
  CopyButton,
  EmptyState,
  ErrorState,
  Input,
  LogView,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SegmentedControl,
  StatusDot,
  Switch,
  host,
  pluginSettingsHref,
  queryClient,
  relativeTime,
  usePluginI18n,
  useQuery,
} from '@hermes/plugin-sdk'

const PLUGIN_ID = 'database-connector'
const QK = [PLUGIN_ID]
const PAGE_PATH = '/databases'
const EVENT_PREFIX = 'plugin.database-connector.'
// Every event is an invalidation signal only (design 2); the 5 s refetch
// intervals below reconcile whatever the fire-and-forget bus dropped.
const INVALIDATING_EVENTS = [
  'query.started',
  'query.finished',
  'query.failed',
  'connection.changed',
]

const ENGINES = ['sqlserver', 'postgres', 'redis']
const ENVIRONMENTS = ['prod', 'homolog', 'local']
const ENV_VARIANT = { prod: 'destructive', homolog: 'warn', local: 'muted' }
const SECRET_VARIANT = { resolved: 'success', missing: 'destructive', unscoped: 'warn' }
const STATUS_VARIANT = { running: 'default', ok: 'success', error: 'destructive' }
const STATEMENT_DISPLAY_CHARS = 120

// ------------------------------------------------------------ locale bundles

const EN = {
  nav: { bancos: 'Databases' },
  palette: { abrir: 'Open Databases' },
  chip: {
    unavailable: 'databases unavailable',
    label: (n, m) => n + (n === 1 ? ' query' : ' queries') + ' · ' + m + (m === 1 ? ' database' : ' databases'),
    detailNone: 'no active queries',
    detailConn: (n, id) => n + ' on ' + id,
    titlePrefix: 'Databases: ',
    ariaPrefix: 'Database activity: ',
  },
  engines: { sqlserver: 'SQL Server', postgres: 'PostgreSQL', redis: 'Redis' },
  tabs: { activity: 'Activity', connections: 'Connections', drivers: 'Drivers' },
  page: {
    title: 'Databases',
    sectionConnections: 'Connections (last access)',
    sectionQueries: 'Recent queries',
    refreshHint: 'refreshes every 5 s',
    connActive: 'active query',
    connIdle: 'idle',
    connDisabled: ' · disabled',
    secretSuffix: s => ' · secret ' + s,
    lastAccess: 'Last access',
    backendTitle: 'Plugin backend unavailable',
    backendDesc: 'The plugin is not answering at /api/plugins/database-connector/. Enable it (plugins.enabled in the profile config), restart the gateway and reopen this page.',
    errorDetail: d => 'Last error: ' + d,
    partial: d => 'Partial failure: ' + d,
  },
  table: {
    time: 'Time', connection: 'Connection', environment: 'Environment', engine: 'Engine',
    statement: 'Statement', duration: 'Duration', status: 'Status', state: 'State',
  },
  empty: {
    noConnectionsTitle: 'No connections configured',
    noConnectionsDesc: 'Create connections in Settings ▸ Plugins ▸ Connections.',
    noQueriesTitle: 'No queries yet',
    noQueriesDesc: 'Agent queries (db_query / db_redis_command) show up here in real time.',
  },
  form: {
    newTitle: 'New connection',
    editTitle: 'Edit connection',
    idLabel: 'id', idHint: 'unique slug, e.g. prod-sqlserver',
    labelLabel: 'Display name', labelHint: 'optional',
    engineLabel: 'Engine', envLabel: 'Environment', envHint: 'no default',
    envPlaceholder: 'select (no default)', selectPlaceholder: 'select',
    prodBadge: 'production — saving asks for confirmation',
    hostLabel: 'Host', portLabel: 'Port', portHint: 'optional (number)',
    databaseLabel: 'Database', databaseHint: 'required (db/schema)',
    userLabel: 'User', userHint: 'optional; read-only user recommended',
    secretLabel: 'secret_ref', secretHint: 'env var NAME — never the value',
    enabledLabel: 'Enabled',
    optionsTitle: 'Engine options',
    driverLabel: 'Driver', driverHint: 'pymssql (default) or pyodbc',
    instanceLabel: 'Instance', instanceHint: 'not applied in this version (pymssql uses host:port)',
    encryptLabel: 'Encrypt', encryptHint: 'driver-fixed: pyodbc uses Encrypt=yes; pymssql via freetds.conf',
    trustLabel: 'Trust the server certificate',
    schemaLabel: 'Schema', schemaHint: 'not applied in this version (no search_path)',
    sslmodeLabel: 'sslmode', sslmodeHint: 'driver default when empty',
    dbIndexLabel: 'db_index', dbIndexHint: 'default 0',
    tlsLabel: 'TLS',
    save: 'Save', cancel: 'Cancel',
    duplicate: 'Warning: a connection with the same engine + environment + host + database already exists.',
    missingFields: fields => 'Fill the required fields: ' + fields,
    confirmProdTitle: 'Save production connection?',
    confirmProdDesc: id => 'Connection "' + id + '" will be stored with environment prod.',
    confirmProdLabel: 'Save in production',
    confirmFailed: 'Failed to save the connection — see the message on the form.',
  },
  list: {
    title: 'Connections',
    test: 'Test', edit: 'Edit', remove: 'Delete', create: 'New connection',
    testing: 'testing…',
    secretBadge: s => 'secret: ' + s,
    envHint: 'Set the env var in Settings ▸ Environment. ',
    openSettings: 'Open Settings',
    deleteTitle: 'Delete connection?',
    deleteDesc: id => 'Connection "' + id + '" and its whole query log will be deleted (cascade).',
    deleteLabel: 'Delete',
    deleteFailed: d => 'Failed to delete: ' + d,
    emptyTitle: 'No connections',
    emptyDesc: 'Create the first connection so the agent can query databases.',
    unknownError: 'unknown',
    failed: d => 'failed: ' + d,
  },
  drivers: {
    title: 'Drivers',
    available: 'available', missing: 'missing',
    moduleSuffix: m => ' (module ' + m + ')',
    withModule: m => ' · ' + m,
    installNote: 'after installing, restart the gateway.',
    copyTitle: 'Copy installation command',
    note: 'Drivers come with the plugin install (python_dependencies); this screen only guides a host that is missing one of them.',
    loading: 'loading…',
    healthErrorTitle: 'Health unavailable',
    healthErrorDesc: 'GET /health failed — the plugin backend must be enabled and the gateway restarted.',
  },
  misc: {
    notRegistered: 'plugin database-connector not registered',
    unknownFailure: 'unknown failure',
    openSettings: 'Open Settings',
  },
}

const PT_BR = {
  nav: { bancos: 'Bancos' },
  palette: { abrir: 'Abrir Bancos' },
  chip: {
    unavailable: 'bancos indisponível',
    label: (n, m) => n + (n === 1 ? ' consulta' : ' consultas') + ' · ' + m + (m === 1 ? ' banco' : ' bancos'),
    detailNone: 'nenhuma consulta ativa',
    detailConn: (n, id) => n + ' em ' + id,
    titlePrefix: 'Bancos: ',
    ariaPrefix: 'Atividade de bancos: ',
  },
  engines: { sqlserver: 'SQL Server', postgres: 'PostgreSQL', redis: 'Redis' },
  tabs: { activity: 'Atividade', connections: 'Conexões', drivers: 'Drivers' },
  page: {
    title: 'Bancos',
    sectionConnections: 'Conexões (último acesso)',
    sectionQueries: 'Consultas recentes',
    refreshHint: 'atualiza a cada 5 s',
    connActive: 'consulta ativa',
    connIdle: 'ociosa',
    connDisabled: ' · desabilitada',
    secretSuffix: s => ' · segredo ' + s,
    lastAccess: 'Último acesso',
    backendTitle: 'Backend do plugin indisponível',
    backendDesc: 'O plugin não responde em /api/plugins/database-connector/. Habilite o plugin (plugins.enabled no config do perfil), reinicie o gateway e reabra a página.',
    errorDetail: d => 'Último erro: ' + d,
    partial: d => 'Falha parcial: ' + d,
  },
  table: {
    time: 'Hora', connection: 'Conexão', environment: 'Ambiente', engine: 'Engine',
    statement: 'Statement', duration: 'Duração', status: 'Status', state: 'Estado',
  },
  empty: {
    noConnectionsTitle: 'Nenhuma conexão configurada',
    noConnectionsDesc: 'Crie conexões em Settings ▸ Plugins ▸ Conexões.',
    noQueriesTitle: 'Nenhuma consulta registrada',
    noQueriesDesc: 'Consultas do agente (db_query / db_redis_command) aparecem aqui em tempo real.',
  },
  form: {
    newTitle: 'Nova conexão',
    editTitle: 'Editar conexão',
    idLabel: 'id', idHint: 'slug único, ex.: prod-sqlserver',
    labelLabel: 'Nome de exibição', labelHint: 'opcional',
    engineLabel: 'Engine', envLabel: 'Ambiente', envHint: 'sem padrão',
    envPlaceholder: 'selecione (sem padrão)', selectPlaceholder: 'selecione',
    prodBadge: 'produção — exige confirmação ao salvar',
    hostLabel: 'Host', portLabel: 'Porta', portHint: 'opcional (número)',
    databaseLabel: 'Database', databaseHint: 'obrigatório (db/schema)',
    userLabel: 'Usuário', userHint: 'opcional; recomendado somente-leitura',
    secretLabel: 'secret_ref', secretHint: 'NOME da env var — nunca o valor',
    enabledLabel: 'Habilitada',
    optionsTitle: 'Opções da engine',
    driverLabel: 'Driver', driverHint: 'pymssql (padrão) ou pyodbc',
    instanceLabel: 'Instância', instanceHint: 'não aplicada nesta versão (pymssql usa host:porta)',
    encryptLabel: 'Encrypt', encryptHint: 'fixo do driver: pyodbc usa Encrypt=yes; pymssql via freetds.conf',
    trustLabel: 'Confiar no certificado do servidor',
    schemaLabel: 'Schema', schemaHint: 'não aplicado nesta versão (sem search_path)',
    sslmodeLabel: 'sslmode', sslmodeHint: 'padrão do driver quando vazio',
    dbIndexLabel: 'db_index', dbIndexHint: 'padrão 0',
    tlsLabel: 'TLS',
    save: 'Salvar', cancel: 'Cancelar',
    duplicate: 'Aviso: já existe uma conexão com a mesma engine + ambiente + host + database.',
    missingFields: fields => 'Preencha os campos obrigatórios: ' + fields,
    confirmProdTitle: 'Salvar conexão de produção?',
    confirmProdDesc: id => 'A conexão "' + id + '" será gravada com environment prod.',
    confirmProdLabel: 'Salvar em produção',
    confirmFailed: 'Falha ao salvar a conexão — veja a mensagem no formulário.',
  },
  list: {
    title: 'Conexões',
    test: 'Testar', edit: 'Editar', remove: 'Excluir', create: 'Nova conexão',
    testing: 'testando…',
    secretBadge: s => 'segredo: ' + s,
    envHint: 'Defina a env var em Settings ▸ Environment. ',
    openSettings: 'Abrir Settings',
    deleteTitle: 'Excluir conexão?',
    deleteDesc: id => 'A conexão "' + id + '" e todo o seu log de consultas serão apagados (cascade).',
    deleteLabel: 'Excluir',
    deleteFailed: d => 'Falha ao excluir: ' + d,
    emptyTitle: 'Nenhuma conexão',
    emptyDesc: 'Crie a primeira conexão para o agente consultar bancos.',
    unknownError: 'desconhecido',
    failed: d => 'falhou: ' + d,
  },
  drivers: {
    title: 'Drivers',
    available: 'disponível', missing: 'faltando',
    moduleSuffix: m => ' (módulo ' + m + ')',
    withModule: m => ' · ' + m,
    installNote: 'depois de instalar, reinicie o gateway.',
    copyTitle: 'Copiar comando de instalação',
    note: 'Drivers chegam com o install do plugin (python_dependencies); esta tela orienta só o host que ficou sem algum deles.',
    loading: 'carregando…',
    healthErrorTitle: 'Health indisponível',
    healthErrorDesc: 'GET /health falhou — o backend do plugin precisa estar habilitado e o gateway reiniciado.',
  },
  misc: {
    notRegistered: 'plugin database-connector não registrado',
    unknownFailure: 'falha desconhecida',
    openSettings: 'Abrir Settings',
  },
}

const LOCALES = { en: EN, 'pt-BR': PT_BR }

// ctx.rest only exists on the register() context; components capture it here.
let rest = null
function api(path, opts) {
  if (!rest) return Promise.reject(new Error('plugin database-connector not registered'))
  return rest(path, opts)
}

// ---------------------------------------------------------------- queries

const useStatus = () =>
  useQuery({
    queryKey: [...QK, 'status'],
    queryFn: () => api('/status'),
    refetchInterval: 5000,
  })

const useQueriesList = () =>
  useQuery({
    queryKey: [...QK, 'queries'],
    queryFn: () => api('/queries?limit=100'),
    refetchInterval: 5000,
  })

const useConnections = () =>
  useQuery({
    queryKey: [...QK, 'connections'],
    queryFn: () => api('/connections'),
    // Settings reads self-heal: a single failed fetch (gateway briefly down,
    // plugin freshly mounted) must not stick on an open screen forever.
    refetchInterval: 15000,
  })

const useHealth = () =>
  useQuery({
    queryKey: [...QK, 'health'],
    queryFn: () => api('/health'),
    refetchInterval: 15000,
  })

function invalidateAll() {
  queryClient.invalidateQueries({ queryKey: QK })
}

function readError(exc) {
  // REST failures arrive as { error, detail }; ctx.rest may reject with the
  // body packed into exc.message ("409: {json}") — unwrap the trailing JSON.
  const detail = exc && (exc.detail || exc.error || exc.message)
  if (typeof detail === 'string' && detail) {
    const json = detail.match(/\{[\s\S]*\}$/)
    if (json) {
      try {
        const body = JSON.parse(json[0])
        if (body && (body.detail || body.error)) return String(body.detail || body.error)
      } catch {}
    }
    return detail
  }
  return null
}

// Last successfully fetched value — screens stay on stale-good data through
// transient refetch failures instead of flashing error states (the chip/status
// bar still signals the failure via dot tone).
function useLastGood(value) {
  const ref = useRef(null)
  if (value !== null && value !== undefined) ref.current = value
  return ref.current
}

// ---------------------------------------------------------------- helpers

function parseOptions(row) {
  const raw = row && (row.options ?? row.options_json)
  if (!raw) return {}
  if (typeof raw === 'object') return { ...raw }
  try {
    return JSON.parse(raw) || {}
  } catch {
    return {}
  }
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return '—'
  const n = Number(ms)
  if (!Number.isFinite(n)) return '—'
  return n >= 1000 ? (n / 1000).toFixed(1) + ' s' : Math.round(n) + ' ms'
}

function fmtTime(ts) {
  const ms = Date.parse(ts)
  if (!Number.isFinite(ms)) return '—'
  return new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' }).format(ms)
}

function truncate(text, limit) {
  const s = String(text ?? '')
  return s.length > limit ? s.slice(0, limit - 1) + '…' : s
}

const rowStyle = { borderBottom: '1px solid var(--ui-stroke-secondary)' }
const cellStyle = {
  textAlign: 'left',
  padding: '4px 8px',
  fontSize: 12,
  color: 'var(--ui-text-secondary)',
  verticalAlign: 'top',
}
const headCellStyle = { ...cellStyle, color: 'var(--ui-text-tertiary)', fontWeight: 600 }
const mutedStyle = { color: 'var(--ui-text-tertiary)', fontSize: 12 }
const tableStyle = { width: '100%', borderCollapse: 'collapse' }
const controlStyle = { width: '100%', maxWidth: 360 }

function EnvBadge({ environment }) {
  return jsx(Badge, { variant: ENV_VARIANT[environment] || 'outline', children: environment })
}

function TableHead({ columns }) {
  return jsx('thead', {
    children: jsx('tr', {
      children: columns.map((label) => jsx('th', { key: label, style: headCellStyle, children: label })),
    }),
  })
}

function Section({ title, action, children }) {
  return jsxs('section', {
    style: { marginBottom: 24 },
    children: [
      jsxs('div', {
        key: 'header',
        style: { display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 },
        children: [
          jsx('h3', { key: 'title', style: { margin: 0, fontSize: 13 }, children: title }),
          action ?? null,
        ],
      }),
      children,
    ],
  })
}

// ------------------------------------------------------- status bar chip

export function StatusChip() {
  const t = usePluginI18n(PLUGIN_ID)
  const { data: fresh, isError } = useStatus()
  const data = useLastGood(fresh)
  const active = Array.isArray(data?.active) ? data.active : []
  const byConn = new Map()
  for (const row of active) {
    byConn.set(row.connection_id, (byConn.get(row.connection_id) ?? 0) + 1)
  }
  const detail = byConn.size
    ? [...byConn].map(([id, n]) => t('chip.detailConn', n, id)).join(', ')
    : t('chip.detailNone')
  const label = isError && data === null
    ? t('chip.unavailable')
    : t('chip.label', active.length, byConn.size)
  return jsxs('button', {
    onClick: () => host.navigate(PAGE_PATH),
    title: t('chip.titlePrefix') + detail,
    'aria-label': t('chip.ariaPrefix') + detail,
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
      padding: '0 8px',
      background: 'none',
      border: 'none',
      font: 'inherit',
      fontSize: 12,
      color: 'var(--ui-text-secondary)',
      cursor: 'pointer',
    },
    children: [
      jsx(StatusDot, { key: 'dot', tone: isError ? (data === null ? 'bad' : 'warn') : active.length > 0 ? 'good' : 'muted' }),
      label,
    ],
  })
}

// ------------------------------------------------------ activity page

function ConnectionStatesCard({ t, connections, queries, activeIds }) {
  const lastUse = new Map()
  for (const row of queries) {
    if (!lastUse.has(row.connection_id)) lastUse.set(row.connection_id, row.ts)
  }
  if (!connections.length) {
    return jsx(EmptyState, {
      title: t('empty.noConnectionsTitle'),
      description: t('empty.noConnectionsDesc'),
    })
  }
  return jsx('table', {
    style: tableStyle,
    children: [
      jsx(TableHead, {
        key: 'head',
        columns: [t('table.connection'), t('table.environment'), t('table.engine'), t('table.state'), t('page.lastAccess')],
      }),
      jsx('tbody', {
        key: 'body',
        children: connections.map((c) =>
          jsxs('tr', {
            key: c.id,
            style: rowStyle,
            children: [
              jsxs('td', { key: 'label', style: cellStyle, children: [
                c.label !== c.id
                  ? jsxs(Fragment, { key: 'main', children: [c.label, ' ', jsx('span', { key: 'id', style: mutedStyle, children: '(' + c.id + ')' })] })
                  : c.id,
                c.enabled ? null : jsx('span', { key: 'off', style: mutedStyle, children: t('page.connDisabled') }),
              ] }),
              jsx('td', { key: 'env', style: cellStyle, children: jsx(EnvBadge, { environment: c.environment }) }),
              jsx('td', { key: 'engine', style: cellStyle, children: ENGINES.includes(c.engine) ? t('engines.' + c.engine) : c.engine }),
              jsxs('td', { key: 'state', style: cellStyle, children: [
                jsx(StatusDot, { key: 'dot', tone: activeIds.has(c.id) ? 'good' : 'muted' }),
                ' ',
                activeIds.has(c.id) ? t('page.connActive') : t('page.connIdle'),
                c.secret_state && c.secret_state !== 'resolved'
                  ? t('page.secretSuffix', c.secret_state)
                  : null,
              ] }),
              jsx('td', { key: 'last', style: cellStyle, children: lastUse.has(c.id) ? relativeTime(Date.parse(lastUse.get(c.id))) : '—' }),
            ],
          }),
        ),
      }),
    ],
  })
}

export function ActivityPage() {
  const t = usePluginI18n(PLUGIN_ID)
  const [tab, setTab] = useState('activity')
  const statusQ = useStatus()
  const queriesQ = useQueriesList()
  const connsQ = useConnections()
  const statusData = useLastGood(statusQ.data)
  const queriesData = useLastGood(queriesQ.data)
  const connsData = useLastGood(connsQ.data)
  const active = Array.isArray(statusData?.active) ? statusData.active : []
  const activeIds = useMemo(() => new Set(active.map((r) => r.connection_id)), [statusData])
  const queries = Array.isArray(queriesData?.queries) ? queriesData.queries : []
  const connections = Array.isArray(connsData?.connections) ? connsData.connections : []
  // A poll failure only surfaces when we never got data; with a last good
  // snapshot the chip's warn dot is the failure signal (no flashing).
  const statusFailed = statusQ.isError && statusData === null
  const queriesFailed = queriesQ.isError && queriesData === null
  // Full-page error only when BOTH canonical reads fail (no flip-flopping when
  // a single endpoint hiccups); a single failure degrades to a banner.
  if (statusFailed && queriesFailed) {
    const detail = readError(statusQ.error) || readError(queriesQ.error)
    return jsxs('div', { style: { padding: 24 }, children: [
      jsx('h2', { key: 'title', style: { marginTop: 0 }, children: t('page.title') }),
      jsx(ErrorState, {
        key: 'error',
        title: t('page.backendTitle'),
        description:
          t('page.backendDesc') + (detail ? '\n' + t('page.errorDetail', truncate(detail, 160)) : ''),
      }),
    ] })
  }
  const banner = (statusFailed || queriesFailed)
    ? t('page.partial', truncate(readError(statusFailed ? statusQ.error : queriesQ.error) || '', 160))
    : null
  return jsxs('div', { style: { padding: 24 }, children: [
    banner
      ? jsx('div', {
          key: 'banner',
          style: {
            margin: '0 0 12px', padding: '6px 10px', fontSize: 12, borderRadius: 8,
            border: '1px solid var(--ui-stroke-secondary)',
            color: 'var(--ui-text-secondary)',
          },
          children: banner,
        })
      : null,
    jsx('h2', { key: 'title', style: { margin: '0 0 12px' }, children: t('page.title') }),
    jsx(SegmentedControl, {
      key: 'tabs',
      style: { marginBottom: 16 },
      sizing: 'content',
      value: tab,
      onChange: setTab,
      options: [
        { id: 'activity', label: t('tabs.activity') },
        { id: 'connections', label: t('tabs.connections') },
        { id: 'drivers', label: t('tabs.drivers') },
      ],
    }),
    tab === 'activity'
      ? jsxs(Fragment, { children: [
    jsx(Section, {
      key: 'connections',
      title: t('page.sectionConnections'),
      children: jsx(ConnectionStatesCard, { t, connections, queries, activeIds }),
    }),
    jsx(Section, {
      key: 'queries',
      title: t('page.sectionQueries'),
      action: jsx('span', { key: 'hint', style: mutedStyle, children: t('page.refreshHint') }),
      children: queries.length
        ? jsx(LogView, {
            children: jsx('table', {
              style: tableStyle,
              children: [
                jsx(TableHead, {
                  key: 'head',
                  columns: [t('table.time'), t('table.connection'), t('table.environment'), t('table.engine'), t('table.statement'), t('table.duration'), t('table.status')],
                }),
                jsx('tbody', {
                  key: 'body',
                  children: queries.map((q) =>
                    jsxs('tr', {
                      key: q.id,
                      style: rowStyle,
                      children: [
                        jsx('td', { key: 'time', style: cellStyle, title: q.ts, children: fmtTime(q.ts) }),
                        jsx('td', { key: 'conn', style: cellStyle, children: q.connection_id }),
                        jsx('td', { key: 'env', style: cellStyle, children: q.environment }),
                        jsx('td', { key: 'engine', style: cellStyle, children: ENGINES.includes(q.engine) ? t('engines.' + q.engine) : q.engine }),
                        jsx('td', {
                          key: 'stmt',
                          style: { ...cellStyle, color: 'var(--ui-text-tertiary)' },
                          title: q.statement || undefined,
                          children: q.statement ? truncate(q.statement, STATEMENT_DISPLAY_CHARS) : '—',
                        }),
                        jsx('td', { key: 'dur', style: cellStyle, children: fmtDuration(q.duration_ms) }),
                        jsx('td', { key: 'status', style: cellStyle, children: jsx(Badge, { variant: STATUS_VARIANT[q.status] || 'outline', children: q.status }) }),
                      ],
                    }),
                  ),
                }),
              ],
            }),
          })
        : jsx(EmptyState, {
            title: t('empty.noQueriesTitle'),
            description: t('empty.noQueriesDesc'),
          }),
    }),
        ] })
      : tab === 'connections'
        ? jsx(ConnectionsManager, { embedded: true })
        : jsx(DriversPage, { embedded: true }),
  ] })
}

// ------------------------------------------------------- settings pages

function Field({ label, hint, children }) {
  return jsxs('label', {
    style: { display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 10 },
    children: [
      jsxs('span', { key: 'label', style: { fontSize: 12, color: 'var(--ui-text-secondary)' }, children: [
        label,
        hint ? jsx('span', { key: 'hint', style: { ...mutedStyle, marginLeft: 6 }, children: hint }) : null,
      ] }),
      children,
    ],
  })
}

function EngineSelect({ t, value, onChange }) {
  return jsx(Select, {
    value: value || undefined,
    onValueChange: onChange,
    children: [
      jsx(SelectTrigger, { key: 'trigger', style: controlStyle, children: jsx(SelectValue, { placeholder: t('form.selectPlaceholder') }) }),
      jsx(SelectContent, {
        key: 'content',
        children: ENGINES.map((engine) =>
          jsx(SelectItem, { key: engine, value: engine, children: t('engines.' + engine) })),
      }),
    ],
  })
}

function EnvironmentSelect({ t, value, onChange }) {
  return jsx(Select, {
    value: value || undefined,
    onValueChange: onChange,
    children: [
      jsx(SelectTrigger, { key: 'trigger', style: controlStyle, children: jsx(SelectValue, { placeholder: t('form.envPlaceholder') }) }),
      jsx(SelectContent, {
        key: 'content',
        children: ENVIRONMENTS.map((env) => jsx(SelectItem, { key: env, value: env, children: env })),
      }),
    ],
  })
}

// Engine-specific option fields (spec desktop-config-ui: conditional fields).
// Fields the drivers of this version do NOT consume are rendered disabled with
// an honest hint — the user must never believe they configured something the
// backend silently ignores (review P1, 2026-11-17).
function EngineOptionsFields({ t, engine, options, setOption }) {
  if (engine === 'sqlserver') {
    return jsxs(Fragment, { children: [
      jsx(Field, {
        key: 'driver',
        label: t('form.driverLabel'), hint: t('form.driverHint'),
        children: jsx(Input, { style: controlStyle, value: options.driver ?? '', onChange: (e) => setOption('driver', e.target.value) }),
      }),
      jsx(Field, {
        key: 'instance',
        label: t('form.instanceLabel'), hint: t('form.instanceHint'),
        children: jsx(Input, { style: controlStyle, value: options.instance ?? '', disabled: true }),
      }),
      jsx(Field, {
        key: 'encrypt',
        label: t('form.encryptLabel'), hint: t('form.encryptHint'),
        children: jsx(Input, { style: controlStyle, value: options.encrypt ?? '', disabled: true }),
      }),
      jsx(Field, {
        key: 'trust',
        label: t('form.trustLabel'),
        children: jsx(Switch, { checked: !!options.trust_server_certificate, onCheckedChange: (v) => setOption('trust_server_certificate', v) }),
      }),
    ] })
  }
  if (engine === 'postgres') {
    return jsxs(Fragment, { children: [
      jsx(Field, {
        key: 'schema',
        label: t('form.schemaLabel'), hint: t('form.schemaHint'),
        children: jsx(Input, { style: controlStyle, value: options.schema ?? '', disabled: true }),
      }),
      jsx(Field, {
        key: 'sslmode',
        label: t('form.sslmodeLabel'), hint: t('form.sslmodeHint'),
        children: jsx(Select, {
          value: options.sslmode || undefined,
          onValueChange: (v) => setOption('sslmode', v),
          children: [
            jsx(SelectTrigger, { key: 'trigger', style: controlStyle, children: jsx(SelectValue, { placeholder: t('form.selectPlaceholder') }) }),
            jsx(SelectContent, {
              key: 'content',
              children: ['disable', 'prefer', 'require', 'verify-full'].map((mode) =>
                jsx(SelectItem, { key: mode, value: mode, children: mode })),
            }),
          ],
        }),
      }),
    ] })
  }
  if (engine === 'redis') {
    return jsxs(Fragment, { children: [
      jsx(Field, {
        key: 'db_index',
        label: t('form.dbIndexLabel'), hint: t('form.dbIndexHint'),
        children: jsx(Input, { style: controlStyle, value: options.db_index ?? '', onChange: (e) => setOption('db_index', e.target.value) }),
      }),
      jsx(Field, {
        key: 'tls',
        label: t('form.tlsLabel'),
        children: jsx(Switch, { checked: !!options.tls, onCheckedChange: (v) => setOption('tls', v) }),
      }),
    ] })
  }
  return null
}

function requiredMissing(form, options = {}) {
  const missing = []
  if (!form.id.trim()) missing.push('id')
  if (!form.engine) missing.push('engine')
  if (!form.environment) missing.push('environment')
  if (!form.host.trim()) missing.push('host')
  if ((form.engine === 'sqlserver' || form.engine === 'postgres') && !form.database.trim()) {
    missing.push('database')
  }
  const port = String(form.port).trim()
  if (port !== '' && !Number.isFinite(Number(port))) missing.push('port')
  if (form.engine === 'redis') {
    const dbIndex = String(options.db_index ?? '').trim()
    if (dbIndex !== '' && !Number.isFinite(Number(dbIndex))) missing.push('db_index')
  }
  return missing
}

function connectionPayload(form, options) {
  const port = String(form.port).trim()
  const clean = { ...options }
  if (form.engine === 'redis') {
    const dbIndex = String(options.db_index ?? '').trim()
    clean.db_index = dbIndex === '' ? 0 : Number(dbIndex)
  }
  // Drop empty strings: absent optional beats empty value in the backend.
  for (const key of Object.keys(clean)) {
    if (clean[key] === '' || clean[key] === undefined) delete clean[key]
  }
  return {
    id: form.id.trim(),
    label: form.label.trim() || form.id.trim(),
    engine: form.engine,
    environment: form.environment,
    host: form.host.trim(),
    port: port === '' ? null : Number(port),
    database: form.database.trim() || null,
    username: form.username.trim() || null,
    secret_ref: form.secret_ref.trim() || null,
    // MVP is read-only by construction; editing never flips a stored value.
    read_only: true,
    enabled: form.enabled,
    options: clean,
  }
}

export function ConnectionForm({ initial, connections, onDone }) {
  const t = usePluginI18n(PLUGIN_ID)
  const editing = !!(initial && initial.id)
  const [form, setForm] = useState(() => ({
    id: initial?.id ?? '',
    label: initial?.label ?? '',
    engine: initial?.engine ?? '',
    environment: initial?.environment ?? '',
    host: initial?.host ?? '',
    port: initial?.port ?? '',
    database: initial?.database ?? '',
    username: initial?.username ?? '',
    secret_ref: initial?.secret_ref ?? '',
    enabled: initial ? !!initial.enabled : true,
  }))
  const [options, setOptions] = useState(() => parseOptions(initial))
  const [error, setError] = useState(null)
  const [confirmProd, setConfirmProd] = useState(false)
  const setField = (key) => (value) => setForm((f) => ({ ...f, [key]: value }))
  const setOption = (key, value) => setOptions((o) => ({ ...o, [key]: value }))

  const duplicate = useMemo(() => {
    if (!form.engine || !form.environment) return false
    return connections.some((c) =>
      c.id !== form.id &&
      c.engine === form.engine &&
      c.environment === form.environment &&
      c.host === form.host.trim() &&
      (c.database ?? '') === form.database.trim())
  }, [connections, form])

  // Returns true when saved — ConfirmDialog's onConfirm throws on false so a
  // failed save keeps the dialog open instead of closing as "done".
  const save = useCallback(async () => {
    setError(null)
    const missing = requiredMissing(form, options)
    if (missing.length) {
      setError(t('form.missingFields', missing.join(', ')))
      return false
    }
    const body = connectionPayload(form, options)
    try {
      if (editing) {
        await api('/connections/' + encodeURIComponent(editing), { method: 'PUT', body })
      } else {
        await api('/connections', { method: 'POST', body })
      }
      invalidateAll()
      onDone()
      return true
    } catch (exc) {
      setError(readError(exc) || 'error')
      return false
    }
  }, [form, options, editing, onDone, t])

  const submit = () => {
    if (requiredMissing(form, options).length) {
      void save()
      return
    }
    // Explicit confirmation before writing a prod connection (spec 3.3).
    if (form.environment === 'prod') setConfirmProd(true)
    else void save()
  }

  return jsxs('div', { children: [
    jsx('h3', { key: 'title', style: { marginTop: 0 }, children: editing ? t('form.editTitle') : t('form.newTitle') }),
    jsxs('div', { key: 'fields', style: { display: 'flex', gap: 16, flexWrap: 'wrap' }, children: [
      jsxs('div', { key: 'common', children: [
        jsx(Field, {
          key: 'id', label: t('form.idLabel'), hint: t('form.idHint'),
          children: jsx(Input, { style: controlStyle, value: form.id, disabled: editing, onChange: (e) => setField('id')(e.target.value) }),
        }),
        jsx(Field, {
          key: 'label', label: t('form.labelLabel'), hint: t('form.labelHint'),
          children: jsx(Input, { style: controlStyle, value: form.label, onChange: (e) => setField('label')(e.target.value) }),
        }),
        jsx(Field, { key: 'engine', label: t('form.engineLabel'), children: jsx(EngineSelect, { t, value: form.engine, onChange: setField('engine') }) }),
        jsx(Field, { key: 'environment', label: t('form.envLabel'), hint: t('form.envHint'), children: jsx(EnvironmentSelect, { t, value: form.environment, onChange: setField('environment') }) }),
        form.environment === 'prod'
          ? jsx('div', { key: 'prodbadge', style: { marginBottom: 10 }, children: jsx(Badge, { variant: 'destructive', children: t('form.prodBadge') }) })
          : null,
        jsx(Field, {
          key: 'host', label: t('form.hostLabel'),
          children: jsx(Input, { style: controlStyle, value: form.host, onChange: (e) => setField('host')(e.target.value) }),
        }),
        jsx(Field, {
          key: 'port', label: t('form.portLabel'), hint: t('form.portHint'),
          children: jsx(Input, { style: controlStyle, value: form.port, onChange: (e) => setField('port')(e.target.value) }),
        }),
        form.engine === 'redis'
          ? null
          : jsx(Field, {
              key: 'database', label: t('form.databaseLabel'), hint: t('form.databaseHint'),
              children: jsx(Input, { style: controlStyle, value: form.database, onChange: (e) => setField('database')(e.target.value) }),
            }),
        jsx(Field, {
          key: 'username', label: t('form.userLabel'), hint: t('form.userHint'),
          children: jsx(Input, { style: controlStyle, value: form.username, onChange: (e) => setField('username')(e.target.value) }),
        }),
        jsx(Field, {
          key: 'secret', label: t('form.secretLabel'), hint: t('form.secretHint'),
          children: jsx(Input, { style: controlStyle, value: form.secret_ref, onChange: (e) => setField('secret_ref')(e.target.value) }),
        }),
        jsx(Field, {
          key: 'enabled', label: t('form.enabledLabel'),
          children: jsx(Switch, { checked: form.enabled, onCheckedChange: setField('enabled') }),
        }),
      ] }),
      form.engine
        ? jsxs('div', { key: 'engine', children: [
            jsx('div', { key: 'opttitle', style: { fontSize: 12, color: 'var(--ui-text-secondary)', marginBottom: 10 }, children: t('form.optionsTitle') }),
            jsx(EngineOptionsFields, { t, engine: form.engine, options, setOption }),
          ] })
        : null,
    ] }),
    duplicate
      ? jsx('div', {
          key: 'duplicate',
          style: { margin: '8px 0', color: 'var(--ui-text-secondary)', fontSize: 12 },
          children: t('form.duplicate'),
        })
      : null,
    error
      ? jsx('div', { key: 'error', style: { margin: '8px 0', color: 'var(--ui-text-secondary)', fontSize: 12 }, children: error })
      : null,
    jsxs('div', { key: 'actions', style: { display: 'flex', gap: 8 }, children: [
      jsx(Button, { key: 'save', onClick: submit, children: t('form.save') }),
      jsx(Button, { key: 'cancel', variant: 'ghost', onClick: onDone, children: t('form.cancel') }),
    ] }),
    jsx(ConfirmDialog, {
      key: 'confirm',
      open: confirmProd,
      onClose: () => setConfirmProd(false),
      onConfirm: async () => {
        if (!(await save())) {
          // Keep the dialog open; the form behind shows the reason.
          throw new Error(t('form.confirmFailed'))
        }
      },
      title: t('form.confirmProdTitle'),
      description: t('form.confirmProdDesc', form.id || form.label),
      confirmLabel: t('form.confirmProdLabel'),
    }),
  ] })
}

function SecretStateBadge({ t, state }) {
  if (!state) return null
  return jsxs(Fragment, { children: [
    ' ',
    jsx(Badge, { key: 'badge', variant: SECRET_VARIANT[state] || 'outline', children: t('list.secretBadge', state) }),
  ] })
}

export function ConnectionsManager({ connections: connectionsProp }) {
  const t = usePluginI18n(PLUGIN_ID)
  const connsQ = useConnections()
  const fetched = useLastGood(Array.isArray(connsQ.data?.connections) ? connsQ.data.connections : null)
  const connections = connectionsProp ?? fetched ?? []
  const [editing, setEditing] = useState(null) // null | 'new' | connection row
  const [deleting, setDeleting] = useState(null)
  const [tests, setTests] = useState({}) // id -> 'busy' | 'ok (12 ms)' | error string
  const refresh = useCallback(() => invalidateAll(), [])

  const doDelete = async () => {
    const target = deleting
    if (!target) return
    try {
      await api('/connections/' + encodeURIComponent(target.id), { method: 'DELETE' })
      refresh()
    } catch (exc) {
      const detail = readError(exc)
      host.notify?.({ kind: 'error', message: t('list.deleteFailed', detail || '') })
    } finally {
      setDeleting(null)
    }
  }

  const doTest = async (c) => {
    setTests((t2) => ({ ...t2, [c.id]: 'busy' }))
    try {
      const result = await api('/connections/' + encodeURIComponent(c.id) + '/test', { method: 'POST' })
      setTests((t2) => ({
        ...t2,
        [c.id]: result?.ok ? 'ok (' + fmtDuration(result.latency_ms) + ')' : t('list.failed', result?.error ?? t('list.unknownError')),
      }))
    } catch (exc) {
      const detail = readError(exc)
      setTests((t2) => ({ ...t2, [c.id]: t('list.failed', detail || '') }))
    }
  }

  if (editing) {
    return jsx(ConnectionForm, {
      initial: editing === 'new' ? null : editing,
      connections,
      onDone: () => setEditing(null),
    })
  }

  return jsxs('div', { children: [
    jsxs('div', { key: 'header', style: { display: 'flex', alignItems: 'baseline', gap: 12 }, children: [
      jsx('h3', { key: 'title', style: { margin: '0 0 12px' }, children: t('list.title') }),
      jsx(Button, {
        key: 'drivers',
        variant: 'ghost',
        onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID, 'drivers')),
        children: t('page.drivers'),
      }),
    ] }),
    connections.length
      ? jsx('table', {
          key: 'list',
          style: tableStyle,
          children: jsx('tbody', {
            children: connections.map((c) =>
              jsxs('tr', {
                key: c.id,
                style: rowStyle,
                children: [
                  jsxs('td', { key: 'label', style: cellStyle, children: [
                    jsx('strong', { key: 'main', children: c.label !== c.id ? c.label : c.id }),
                    c.label !== c.id ? jsx('span', { key: 'id', style: mutedStyle, children: ' (' + c.id + ')' }) : null,
                  ] }),
                  jsxs('td', { key: 'badges', style: cellStyle, children: [
                    jsx(Badge, { key: 'engine', variant: 'outline', children: ENGINES.includes(c.engine) ? t('engines.' + c.engine) : c.engine }),
                    ' ',
                    jsx(EnvBadge, { key: 'env', environment: c.environment }),
                    jsx(SecretStateBadge, { key: 'secret', t, state: c.secret_state }),
                    c.secret_state && c.secret_state !== 'resolved'
                      ? jsxs('div', { key: 'hint', style: mutedStyle, children: [
                          t('list.envHint'),
                          jsx('button', {
                            key: 'open',
                            style: {
                              background: 'none',
                              border: 'none',
                              padding: 0,
                              color: 'var(--ui-accent)',
                              cursor: 'pointer',
                              font: 'inherit',
                              fontSize: 12,
                            },
                            onClick: () => host.navigate('/settings'),
                            children: t('list.openSettings'),
                          }),
                        ] })
                      : null,
                  ] }),
                  jsxs('td', { key: 'target', style: cellStyle, children: [
                    jsx('span', { key: 'host', style: mutedStyle, children: c.host + (c.port ? ':' + c.port : '') }),
                    c.database ? jsx('span', { key: 'db', style: mutedStyle, children: ' · ' + c.database }) : null,
                    jsx('div', { key: 'test', style: mutedStyle, children: tests[c.id] === 'busy' ? t('list.testing') : (tests[c.id] ?? '') }),
                  ] }),
                  jsxs('td', { key: 'actions', style: cellStyle, children: [
                    jsxs('div', { key: 'row', style: { display: 'flex', gap: 6 }, children: [
                      jsx(Button, { key: 'test', variant: 'secondary', onClick: () => void doTest(c), children: t('list.test') }),
                      jsx(Button, { key: 'edit', variant: 'ghost', onClick: () => setEditing(c), children: t('list.edit') }),
                      jsx(Button, { key: 'ghost-delete', variant: 'ghost', onClick: () => setDeleting(c), children: t('list.remove') }),
                    ] }),
                  ] }),
                ],
              }),
            ),
          }),
        })
      : jsx(EmptyState, {
          key: 'empty',
          title: t('list.emptyTitle'),
          description: t('list.emptyDesc'),
        }),
    jsx('div', { key: 'new', style: { marginTop: 12 }, children: jsx(Button, { onClick: () => setEditing('new'), children: t('list.create') }) }),
    jsx(ConfirmDialog, {
      key: 'confirm',
      open: !!deleting,
      onClose: () => setDeleting(null),
      onConfirm: doDelete,
      destructive: true,
      title: t('list.deleteTitle'),
      description: t('list.deleteDesc', deleting?.id ?? ''),
      confirmLabel: t('list.deleteLabel'),
    }),
  ] })
}

export function DriversPage({ health }) {
  const t = usePluginI18n(PLUGIN_ID)
  const healthQ = useHealth()
  const engines = health?.engines ?? healthQ.data?.engines ?? useLastGood(healthQ.data?.engines)
  if (!engines) {
    if (healthQ.isError) {
      const detail = readError(healthQ.error)
      return jsx(ErrorState, {
        title: t('drivers.healthErrorTitle'),
        description: t('drivers.healthErrorDesc') + (detail ? '\n' + t('page.errorDetail', truncate(detail, 160)) : ''),
      })
    }
    return jsx('div', { style: mutedStyle, children: t('drivers.loading') })
  }
  return jsxs('div', { children: [
    jsx('h3', { key: 'title', style: { marginTop: 0 }, children: t('drivers.title') }),
    jsx('table', {
      key: 'table',
      style: tableStyle,
      children: jsx('tbody', {
        children: ENGINES.map((engine) => {
          const info = engines[engine] || {}
          return jsxs('tr', {
            key: engine,
            style: rowStyle,
            children: [
              jsxs('td', { key: 'name', style: cellStyle, children: [
                jsx(StatusDot, { key: 'dot', tone: info.available ? 'good' : 'bad' }),
                ' ',
                jsx('strong', { key: 'label', children: ENGINES.includes(engine) ? t('engines.' + engine) : engine }),
              ] }),
              jsxs('td', { key: 'status', style: cellStyle, children: [
                info.available
                  ? t('drivers.available') + (info.module ? t('drivers.withModule', info.module) : '')
                  : jsxs(Fragment, { key: 'missing', children: [
                      t('drivers.missing'),
                      info.module ? jsx('span', { key: 'module', style: mutedStyle, children: t('drivers.moduleSuffix', info.module) }) : null,
                    ] }),
              ] }),
              jsxs('td', { key: 'install', style: cellStyle, children: [
                info.available || !info.install
                  ? null
                  : jsxs(Fragment, { key: 'cmd', children: [
                      jsx('code', { key: 'code', style: { fontSize: 11 }, children: info.install }),
                      ' ',
                      jsx(CopyButton, { key: 'copy', text: info.install, title: t('drivers.copyTitle') }),
                      jsx('div', { key: 'note', style: mutedStyle, children: t('drivers.installNote') }),
                    ] }),
              ] }),
            ],
          })
        }),
      }),
    }),
    jsx('p', {
      key: 'note',
      style: mutedStyle,
      children: t('drivers.note'),
    }),
  ] })
}

// ------------------------------------------------------------------ wiring

export default {
  id: PLUGIN_ID,
  name: 'Database Connector',
  register(ctx) {
    rest = ctx.rest.bind(ctx)
    // Plugin-scoped locale bundles (en fallback + pt-BR), dropped by the host
    // on unload/reload/disable together with every other registration.
    ctx.i18n?.register?.(LOCALES)
    // Nothing long-lived here: onEvent disposers and onDispose are retired by
    // the host on unload/reload/disable (SDK contract).
    for (const name of INVALIDATING_EVENTS) {
      ctx.onEvent(EVENT_PREFIX + name, () => {
        queryClient.invalidateQueries({ queryKey: QK })
      })
    }
    ctx.onDispose(() => {
      rest = null
    })
    // Static surfaces (sidebar row + palette row) carry translated labels;
    // rebuild them when the user switches locale.
    let navDisposer = null
    let paletteDisposer = null
    const registerStatic = () => {
      if (navDisposer) navDisposer()
      if (paletteDisposer) paletteDisposer()
      navDisposer = ctx.register({
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PAGE_PATH, label: ctx.i18n.t('nav.bancos'), codicon: 'database' },
      })
      paletteDisposer = ctx.register({
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'databases.open',
          label: ctx.i18n.t('palette.abrir'),
          keywords: ['bancos', 'databases', 'sql', 'consultas', 'queries', 'redis'],
          run: () => host.navigate(PAGE_PATH),
        },
      })
    }
    registerStatic()
    ctx.i18n?.onLocaleChange?.(registerStatic)
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PAGE_PATH }, render: () => jsx(ActivityPage, {}) },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 120, render: () => jsx(StatusChip, {}) },
    ])
    ctx.registerSettingsPage?.({
      id: 'connections',
      title: ctx.i18n.t('list.title'),
      icon: 'database',
      render: () => jsx(ConnectionsManager, {}),
      children: [{ id: 'drivers', title: ctx.i18n.t('drivers.title'), render: () => jsx(DriversPage, {}) }],
    })
  },
}
