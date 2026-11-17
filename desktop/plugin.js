// database-connector — desktop half (Hermes Desktop Plugin SDK).
// ESM on disk, loaded uncompiled: no JSX syntax, no build step, no hex colors,
// no window timers. Children always go through props (jsx/jsxs contract).
// State: useQuery + shared queryClient; events only invalidate — REST
// (refetch + 5 s polling) is the source of truth.
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useMemo, useState } from 'react'
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
  StatusDot,
  Switch,
  host,
  pluginSettingsHref,
  queryClient,
  relativeTime,
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
const ENGINE_LABEL = { sqlserver: 'SQL Server', postgres: 'PostgreSQL', redis: 'Redis' }
const ENVIRONMENTS = ['prod', 'homolog', 'local']
const ENV_VARIANT = { prod: 'destructive', homolog: 'warn', local: 'muted' }
const SECRET_VARIANT = { resolved: 'success', missing: 'destructive', unscoped: 'warn' }
const STATUS_VARIANT = { running: 'default', ok: 'success', error: 'destructive' }
const STATEMENT_DISPLAY_CHARS = 120

// ctx.rest only exists on the register() context; components capture it here.
let rest = null
function api(path, opts) {
  if (!rest) return Promise.reject(new Error('plugin database-connector não registrado'))
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
  useQuery({ queryKey: [...QK, 'connections'], queryFn: () => api('/connections') })

const useHealth = () =>
  useQuery({ queryKey: [...QK, 'health'], queryFn: () => api('/health') })

function invalidateAll() {
  queryClient.invalidateQueries({ queryKey: QK })
}

async function readError(exc) {
  // REST failures arrive as { error, detail }; ctx.rest may reject with either.
  const detail = exc && (exc.detail || exc.error || exc.message)
  return typeof detail === 'string' && detail ? detail : 'falha desconhecida'
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
        style: { display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 },
        children: [
          jsx('h3', { style: { margin: 0, fontSize: 13 }, children: title }),
          action ?? null,
        ],
      }),
      children,
    ],
  })
}

// ------------------------------------------------------- status bar chip

export function StatusChip() {
  const { data, isError } = useStatus()
  const active = Array.isArray(data?.active) ? data.active : []
  const byConn = new Map()
  for (const row of active) {
    byConn.set(row.connection_id, (byConn.get(row.connection_id) ?? 0) + 1)
  }
  const detail = byConn.size
    ? [...byConn].map(([id, n]) => n + ' em ' + id).join(', ')
    : 'nenhuma consulta ativa'
  const label = isError
    ? 'bancos indisponível'
    : active.length + (active.length === 1 ? ' consulta' : ' consultas') +
      ' · ' + byConn.size + (byConn.size === 1 ? ' banco' : ' bancos')
  return jsxs('button', {
    onClick: () => host.navigate(PAGE_PATH),
    title: 'Bancos: ' + detail,
    'aria-label': 'Atividade de bancos: ' + detail,
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
      jsx(StatusDot, { tone: isError ? 'bad' : active.length > 0 ? 'good' : 'muted' }),
      label,
    ],
  })
}

// ------------------------------------------------------ activity page

function ConnectionStatesCard({ connections, queries, activeIds }) {
  const lastUse = new Map()
  for (const row of queries) {
    if (!lastUse.has(row.connection_id)) lastUse.set(row.connection_id, row.ts)
  }
  if (!connections.length) {
    return jsx(EmptyState, {
      title: 'Nenhuma conexão configurada',
      description: 'Crie conexões em Settings ▸ Plugins ▸ Conexões.',
    })
  }
  return jsx('table', {
    style: tableStyle,
    children: [
      jsx(TableHead, {
        columns: ['Conexão', 'Ambiente', 'Engine', 'Estado', 'Último acesso'],
      }),
      jsx('tbody', {
        children: connections.map((c) =>
          jsxs('tr', {
            key: c.id,
            style: rowStyle,
            children: [
              jsxs('td', { style: cellStyle, children: [
                c.label !== c.id
                  ? jsxs(Fragment, { children: [c.label, ' ', jsx('span', { style: mutedStyle, children: '(' + c.id + ')' })] })
                  : c.id,
                c.enabled ? null : jsx('span', { style: mutedStyle, children: ' · desabilitada' }),
              ] }),
              jsx('td', { style: cellStyle, children: jsx(EnvBadge, { environment: c.environment }) }),
              jsx('td', { style: cellStyle, children: ENGINE_LABEL[c.engine] || c.engine }),
              jsxs('td', { style: cellStyle, children: [
                jsx(StatusDot, { tone: activeIds.has(c.id) ? 'good' : 'muted' }),
                ' ',
                activeIds.has(c.id) ? 'consulta ativa' : 'ociosa',
                c.secret_state && c.secret_state !== 'resolved'
                  ? ' · segredo ' + c.secret_state
                  : null,
              ] }),
              jsx('td', { style: cellStyle, children: lastUse.has(c.id) ? relativeTime(Date.parse(lastUse.get(c.id))) : '—' }),
            ],
          }),
        ),
      }),
    ],
  })
}

export function ActivityPage() {
  const statusQ = useStatus()
  const queriesQ = useQueriesList()
  const connsQ = useConnections()
  const active = Array.isArray(statusQ.data?.active) ? statusQ.data.active : []
  const activeIds = useMemo(() => new Set(active.map((r) => r.connection_id)), [statusQ.data])
  const queries = Array.isArray(queriesQ.data?.queries) ? queriesQ.data.queries : []
  const connections = Array.isArray(connsQ.data?.connections) ? connsQ.data.connections : []
  const failed = statusQ.isError || queriesQ.isError
  if (failed) {
    return jsxs('div', { style: { padding: 24 }, children: [
      jsx('h2', { style: { marginTop: 0 }, children: 'Bancos' }),
      jsx(ErrorState, {
        title: 'Backend do plugin indisponível',
        description:
          'O plugin não responde em /api/plugins/database-connector/. ' +
          'Habilite o plugin (plugins.enabled no config do perfil), reinicie o gateway e reabra a página.',
      }),
    ] })
  }
  return jsxs('div', { style: { padding: 24 }, children: [
    jsxs('div', { style: { display: 'flex', alignItems: 'baseline', gap: 12 }, children: [
      jsx('h2', { style: { margin: '0 0 16px' }, children: 'Bancos' }),
      jsx(Button, {
        variant: 'ghost',
        onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID, 'drivers')),
        children: 'Drivers',
      }),
      jsx(Button, {
        variant: 'ghost',
        onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID, 'connections')),
        children: 'Conexões',
      }),
    ] }),
    jsx(Section, {
      title: 'Conexões (último acesso)',
      children: jsx(ConnectionStatesCard, { connections, queries, activeIds }),
    }),
    jsx(Section, {
      title: 'Consultas recentes',
      action: jsx('span', { style: mutedStyle, children: 'atualiza a cada 5 s' }),
      children: queries.length
        ? jsx(LogView, {
            children: jsx('table', {
              style: tableStyle,
              children: [
                jsx(TableHead, {
                  columns: ['Hora', 'Conexão', 'Ambiente', 'Engine', 'Statement', 'Duração', 'Status'],
                }),
                jsx('tbody', {
                  children: queries.map((q) =>
                    jsxs('tr', {
                      key: q.id,
                      style: rowStyle,
                      children: [
                        jsx('td', { style: cellStyle, title: q.ts, children: fmtTime(q.ts) }),
                        jsx('td', { style: cellStyle, children: q.connection_id }),
                        jsx('td', { style: cellStyle, children: q.environment }),
                        jsx('td', { style: cellStyle, children: ENGINE_LABEL[q.engine] || q.engine }),
                        jsx('td', {
                          style: { ...cellStyle, color: 'var(--ui-text-tertiary)' },
                          title: q.statement || undefined,
                          children: q.statement ? truncate(q.statement, STATEMENT_DISPLAY_CHARS) : '—',
                        }),
                        jsx('td', { style: cellStyle, children: fmtDuration(q.duration_ms) }),
                        jsx('td', { style: cellStyle, children: jsx(Badge, { variant: STATUS_VARIANT[q.status] || 'outline', children: q.status }) }),
                      ],
                    }),
                  ),
                }),
              ],
            }),
          })
        : jsx(EmptyState, {
            title: 'Nenhuma consulta registrada',
            description: 'Consultas do agente (db_query / db_redis_command) aparecem aqui em tempo real.',
          }),
    }),
  ] })
}

// ------------------------------------------------------- settings pages

function Field({ label, hint, children }) {
  return jsxs('label', {
    style: { display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 10 },
    children: [
      jsxs('span', { style: { fontSize: 12, color: 'var(--ui-text-secondary)' }, children: [
        label,
        hint ? jsx('span', { style: { ...mutedStyle, marginLeft: 6 }, children: hint }) : null,
      ] }),
      children,
    ],
  })
}

function EngineSelect({ value, onChange }) {
  return jsx(Select, {
    value: value || undefined,
    onValueChange: onChange,
    children: [
      jsx(SelectTrigger, { key: 't', style: controlStyle, children: jsx(SelectValue, { placeholder: 'selecione' }) }),
      jsx(SelectContent, {
        key: 'c',
        children: ENGINES.map((engine) =>
          jsx(SelectItem, { key: engine, value: engine, children: ENGINE_LABEL[engine] })),
      }),
    ],
  })
}

function EnvironmentSelect({ value, onChange }) {
  return jsx(Select, {
    value: value || undefined,
    onValueChange: onChange,
    children: [
      jsx(SelectTrigger, { key: 't', style: controlStyle, children: jsx(SelectValue, { placeholder: 'selecione (sem padrão)' }) }),
      jsx(SelectContent, {
        key: 'c',
        children: ENVIRONMENTS.map((env) => jsx(SelectItem, { key: env, value: env, children: env })),
      }),
    ],
  })
}

// Engine-specific option fields (spec desktop-config-ui: conditional fields).
function EngineOptionsFields({ engine, options, setOption }) {
  if (engine === 'sqlserver') {
    return jsxs(Fragment, { children: [
      jsx(Field, {
        label: 'Driver ODBC',
        hint: 'opcional (caminho pyodbc)',
        children: jsx(Input, { style: controlStyle, value: options.driver ?? '', onChange: (e) => setOption('driver', e.target.value) }),
      }),
      jsx(Field, {
        label: 'Instância',
        hint: 'opcional',
        children: jsx(Input, { style: controlStyle, value: options.instance ?? '', onChange: (e) => setOption('instance', e.target.value) }),
      }),
      jsx(Field, {
        label: 'Encrypt',
        hint: 'ex.: yes',
        children: jsx(Input, { style: controlStyle, value: options.encrypt ?? '', onChange: (e) => setOption('encrypt', e.target.value) }),
      }),
      jsx(Field, {
        label: 'Confiar no certificado do servidor',
        children: jsx(Switch, { checked: !!options.trust_server_certificate, onCheckedChange: (v) => setOption('trust_server_certificate', v) }),
      }),
    ] })
  }
  if (engine === 'postgres') {
    return jsxs(Fragment, { children: [
      jsx(Field, {
        label: 'Schema',
        hint: 'opcional',
        children: jsx(Input, { style: controlStyle, value: options.schema ?? '', onChange: (e) => setOption('schema', e.target.value) }),
      }),
      jsx(Field, {
        label: 'sslmode',
        hint: 'padrão do driver quando vazio',
        children: jsx(Select, {
          value: options.sslmode || undefined,
          onValueChange: (v) => setOption('sslmode', v),
          children: [
            jsx(SelectTrigger, { key: 't', style: controlStyle, children: jsx(SelectValue, { placeholder: 'selecione' }) }),
            jsx(SelectContent, {
              key: 'c',
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
        label: 'db_index',
        hint: 'padrão 0',
        children: jsx(Input, { style: controlStyle, value: options.db_index ?? '', onChange: (e) => setOption('db_index', e.target.value) }),
      }),
      jsx(Field, {
        label: 'TLS',
        children: jsx(Switch, { checked: !!options.tls, onCheckedChange: (v) => setOption('tls', v) }),
      }),
    ] })
  }
  return null
}

function requiredMissing(form) {
  const missing = []
  if (!form.id.trim()) missing.push('id')
  if (!form.engine) missing.push('engine')
  if (!form.environment) missing.push('environment')
  if (!form.host.trim()) missing.push('host')
  if ((form.engine === 'sqlserver' || form.engine === 'postgres') && !form.database.trim()) {
    missing.push('database')
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
    read_only: true,
    enabled: form.enabled,
    options: clean,
  }
}

export function ConnectionForm({ initial, connections, onDone }) {
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

  const save = useCallback(async () => {
    setError(null)
    const missing = requiredMissing(form)
    if (missing.length) {
      setError('Preencha os campos obrigatórios: ' + missing.join(', '))
      return
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
    } catch (exc) {
      setError(await readError(exc))
    }
  }, [form, options, editing, onDone])

  const submit = () => {
    if (requiredMissing(form).length) {
      void save()
      return
    }
    // Explicit confirmation before writing a prod connection (spec 3.3).
    if (form.environment === 'prod') setConfirmProd(true)
    else void save()
  }

  return jsxs('div', { children: [
    jsx('h3', { style: { marginTop: 0 }, children: editing ? 'Editar conexão' : 'Nova conexão' }),
    jsxs('div', { style: { display: 'flex', gap: 16, flexWrap: 'wrap' }, children: [
      jsxs('div', { children: [
        jsx(Field, {
          label: 'id',
          hint: 'slug único, ex.: prod-sqlserver',
          children: jsx(Input, { style: controlStyle, value: form.id, disabled: editing, onChange: (e) => setField('id')(e.target.value) }),
        }),
        jsx(Field, {
          label: 'Nome de exibição',
          hint: 'opcional',
          children: jsx(Input, { style: controlStyle, value: form.label, onChange: (e) => setField('label')(e.target.value) }),
        }),
        jsx(Field, { label: 'Engine', children: jsx(EngineSelect, { value: form.engine, onChange: setField('engine') }) }),
        jsx(Field, { label: 'Ambiente', hint: 'sem padrão', children: jsx(EnvironmentSelect, { value: form.environment, onChange: setField('environment') }) }),
        form.environment === 'prod'
          ? jsx('div', { style: { marginBottom: 10 }, children: jsx(Badge, { variant: 'destructive', children: 'produção — exige confirmação ao salvar' }) })
          : null,
        jsx(Field, {
          label: 'Host',
          children: jsx(Input, { style: controlStyle, value: form.host, onChange: (e) => setField('host')(e.target.value) }),
        }),
        jsx(Field, {
          label: 'Porta',
          hint: 'opcional',
          children: jsx(Input, { style: controlStyle, value: form.port, onChange: (e) => setField('port')(e.target.value) }),
        }),
        form.engine === 'redis'
          ? null
          : jsx(Field, {
              label: 'Database',
              hint: 'obrigatório (db/schema)',
              children: jsx(Input, { style: controlStyle, value: form.database, onChange: (e) => setField('database')(e.target.value) }),
            }),
        jsx(Field, {
          label: 'Usuário',
          hint: 'opcional; recomendado somente-leitura',
          children: jsx(Input, { style: controlStyle, value: form.username, onChange: (e) => setField('username')(e.target.value) }),
        }),
        jsx(Field, {
          label: 'secret_ref',
          hint: 'NOME da env var — nunca o valor',
          children: jsx(Input, { style: controlStyle, value: form.secret_ref, onChange: (e) => setField('secret_ref')(e.target.value) }),
        }),
        jsx(Field, {
          label: 'Habilitada',
          children: jsx(Switch, { checked: form.enabled, onCheckedChange: setField('enabled') }),
        }),
      ] }),
      form.engine
        ? jsxs('div', { children: [
            jsx('div', { style: { fontSize: 12, color: 'var(--ui-text-secondary)', marginBottom: 10 }, children: 'Opções da engine' }),
            jsx(EngineOptionsFields, { engine: form.engine, options, setOption }),
          ] })
        : null,
    ] }),
    duplicate
      ? jsx('div', {
          style: { margin: '8px 0', color: 'var(--ui-text-secondary)', fontSize: 12 },
          children: 'Aviso: já existe uma conexão com a mesma engine + ambiente + host + database.',
        })
      : null,
    error
      ? jsx('div', { style: { margin: '8px 0', color: 'var(--ui-text-secondary)', fontSize: 12 }, children: error })
      : null,
    jsxs('div', { style: { display: 'flex', gap: 8 }, children: [
      jsx(Button, { onClick: submit, children: 'Salvar' }),
      jsx(Button, { variant: 'ghost', onClick: onDone, children: 'Cancelar' }),
    ] }),
    jsx(ConfirmDialog, {
      open: confirmProd,
      onClose: () => setConfirmProd(false),
      onConfirm: save,
      title: 'Salvar conexão de produção?',
      description: 'A conexão "' + (form.id || form.label) + '" será gravada com environment prod.',
      confirmLabel: 'Salvar em produção',
    }),
  ] })
}

function SecretStateBadge({ state }) {
  if (!state) return null
  return jsxs(Fragment, { children: [
    ' ',
    jsx(Badge, { variant: SECRET_VARIANT[state] || 'outline', children: 'segredo: ' + state }),
  ] })
}

export function ConnectionsManager({ connections: connectionsProp }) {
  const connsQ = useConnections()
  const connections = connectionsProp ??
    (Array.isArray(connsQ.data?.connections) ? connsQ.data.connections : [])
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
      host.notify?.({ kind: 'error', message: 'Falha ao excluir: ' + (await readError(exc)) })
    } finally {
      setDeleting(null)
    }
  }

  const doTest = async (c) => {
    setTests((t) => ({ ...t, [c.id]: 'busy' }))
    try {
      const result = await api('/connections/' + encodeURIComponent(c.id) + '/test', { method: 'POST' })
      setTests((t) => ({
        ...t,
        [c.id]: result?.ok ? 'ok (' + fmtDuration(result.latency_ms) + ')' : 'falhou: ' + (result?.error ?? 'desconhecido'),
      }))
    } catch (exc) {
      const detail = await readError(exc)
      setTests((t) => ({ ...t, [c.id]: 'falhou: ' + detail }))
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
    jsxs('div', { style: { display: 'flex', alignItems: 'baseline', gap: 12 }, children: [
      jsx('h3', { style: { margin: '0 0 12px' }, children: 'Conexões' }),
      jsx(Button, {
        variant: 'ghost',
        onClick: () => host.navigate(pluginSettingsHref(PLUGIN_ID, 'drivers')),
        children: 'Drivers',
      }),
    ] }),
    connections.length
      ? jsx('table', {
          style: tableStyle,
          children: jsx('tbody', {
            children: connections.map((c) =>
              jsxs('tr', {
                key: c.id,
                style: rowStyle,
                children: [
                  jsxs('td', { style: cellStyle, children: [
                    jsx('strong', { children: c.label !== c.id ? c.label : c.id }),
                    c.label !== c.id ? jsx('span', { style: mutedStyle, children: ' (' + c.id + ')' }) : null,
                  ] }),
                  jsxs('td', { style: cellStyle, children: [
                    jsx(Badge, { variant: 'outline', children: ENGINE_LABEL[c.engine] || c.engine }),
                    ' ',
                    jsx(EnvBadge, { environment: c.environment }),
                    jsx(SecretStateBadge, { state: c.secret_state }),
                    c.secret_state && c.secret_state !== 'resolved'
                      ? jsxs('div', { style: mutedStyle, children: [
                          'Defina a env var em Settings ▸ Environment. ',
                          jsx('button', {
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
                            children: 'Abrir Settings',
                          }),
                        ] })
                      : null,
                  ] }),
                  jsxs('td', { style: cellStyle, children: [
                    jsx('span', { style: mutedStyle, children: c.host + (c.port ? ':' + c.port : '') }),
                    c.database ? jsx('span', { style: mutedStyle, children: ' · ' + c.database }) : null,
                    jsx('div', { style: mutedStyle, children: tests[c.id] === 'busy' ? 'testando…' : (tests[c.id] ?? '') }),
                  ] }),
                  jsxs('td', { style: cellStyle, children: [
                    jsxs('div', { style: { display: 'flex', gap: 6 }, children: [
                      jsx(Button, { variant: 'secondary', onClick: () => void doTest(c), children: 'Testar' }),
                      jsx(Button, { variant: 'ghost', onClick: () => setEditing(c), children: 'Editar' }),
                      jsx(Button, { variant: 'ghost', onClick: () => setDeleting(c), children: 'Excluir' }),
                    ] }),
                  ] }),
                ],
              }),
            ),
          }),
        })
      : jsx(EmptyState, {
          title: 'Nenhuma conexão',
          description: 'Crie a primeira conexão para o agente consultar bancos.',
        }),
    jsx('div', { style: { marginTop: 12 }, children: jsx(Button, { onClick: () => setEditing('new'), children: 'Nova conexão' }) }),
    jsx(ConfirmDialog, {
      open: !!deleting,
      onClose: () => setDeleting(null),
      onConfirm: doDelete,
      destructive: true,
      title: 'Excluir conexão?',
      description: 'A conexão "' + (deleting?.id ?? '') + '" e todo o seu log de consultas serão apagados (cascade).',
      confirmLabel: 'Excluir',
    }),
  ] })
}

export function DriversPage({ health }) {
  const healthQ = useHealth()
  const engines = health?.engines ?? healthQ.data?.engines
  if (!engines) {
    if (healthQ.isError) {
      return jsx(ErrorState, {
        title: 'Health indisponível',
        description: 'GET /health falhou — o backend do plugin precisa estar habilitado e o gateway reiniciado.',
      })
    }
    return jsx('div', { style: mutedStyle, children: 'carregando…' })
  }
  return jsxs('div', { children: [
    jsx('h3', { style: { marginTop: 0 }, children: 'Drivers' }),
    jsx('table', {
      style: tableStyle,
      children: jsx('tbody', {
        children: ENGINES.map((engine) => {
          const info = engines[engine] || {}
          return jsxs('tr', {
            key: engine,
            style: rowStyle,
            children: [
              jsxs('td', { style: cellStyle, children: [
                jsx(StatusDot, { tone: info.available ? 'good' : 'bad' }),
                ' ',
                jsx('strong', { children: ENGINE_LABEL[engine] || engine }),
              ] }),
              jsxs('td', { style: cellStyle, children: [
                info.available
                  ? 'disponível'
                  : jsxs(Fragment, { children: [
                      'faltando',
                      info.module ? jsx('span', { style: mutedStyle, children: ' (módulo ' + info.module + ')' }) : null,
                    ] }),
              ] }),
              jsxs('td', { style: cellStyle, children: [
                info.available || !info.install
                  ? null
                  : jsxs(Fragment, { children: [
                      jsx('code', { style: { fontSize: 11 }, children: info.install }),
                      ' ',
                      jsx(CopyButton, { text: info.install, title: 'Copiar comando de instalação' }),
                      jsx('div', { style: mutedStyle, children: 'depois de instalar, reinicie o gateway.' }),
                    ] }),
              ] }),
            ],
          })
        }),
      }),
    }),
    jsx('p', {
      style: mutedStyle,
      children: 'Drivers chegam com o install do plugin (python_dependencies); esta tela orienta só o host que ficou sem algum deles.',
    }),
  ] })
}

// ------------------------------------------------------------------ wiring

export default {
  id: PLUGIN_ID,
  name: 'Database Connector',
  register(ctx) {
    rest = ctx.rest.bind(ctx)
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
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PAGE_PATH }, render: () => jsx(ActivityPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PAGE_PATH, label: 'Bancos', codicon: 'database' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 120, render: () => jsx(StatusChip, {}) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'databases.open',
          label: 'Abrir Bancos',
          keywords: ['bancos', 'database', 'sql', 'consultas', 'redis'],
          run: () => host.navigate(PAGE_PATH),
        },
      },
    ])
    ctx.registerSettingsPage?.({
      id: 'connections',
      title: 'Conexões',
      icon: 'database',
      render: () => jsx(ConnectionsManager, {}),
      children: [{ id: 'drivers', title: 'Drivers', render: () => jsx(DriversPage, {}) }],
    })
  },
}
