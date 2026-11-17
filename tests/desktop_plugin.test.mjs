// Local verification of desktop/plugin.js without the Hermes Desktop app
// (tasks add-desktop-live-ui 2.x-5.1): import, register() wiring, listeners,
// render labels, statement truncation, style/lifecycle greps.
// Run: node --import ./tests/desktop/register-stubs.mjs --test tests/desktop_plugin.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import plugin, {
  ActivityPage,
  StatusChip,
  ConnectionsManager,
  ConnectionForm,
  DriversPage,
} from '../desktop/plugin.js'
import { host, queryClient, queryCalls, resetQueryCalls, setStubData } from './desktop/stubs/plugin-sdk.mjs'

const SOURCE = readFileSync(fileURLToPath(new URL('../desktop/plugin.js', import.meta.url)), 'utf8')

function collectText(node, out = []) {
  if (node === null || node === undefined || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') {
    out.push(String(node))
    return out
  }
  if (Array.isArray(node)) {
    for (const child of node) collectText(child, out)
    return out
  }
  if (typeof node === 'object') {
    const props = node.props ?? {}
    collectText(props.children, out)
    // Function components render like React would — call them with their props
    // (their hooks are stubbed, so this is safe and repeatable).
    if (typeof node.type === 'function') collectText(node.type(props), out)
  }
  return out
}

const text = (node) => collectText(node).join(' ')

const STUB_DATA = {
  ok: true,
  active: [{ id: 2, connection_id: 'prod-sqlserver', status: 'running', ts: '2026-11-17T12:00:00Z' }],
  queries: [
    {
      id: 1,
      ts: '2026-11-17T11:59:00Z',
      query_id: 'q1',
      connection_id: 'prod-sqlserver',
      engine: 'sqlserver',
      environment: 'prod',
      statement: 'S'.repeat(300),
      rows: 5,
      duration_ms: 340,
      status: 'ok',
      source: 'agent',
    },
    {
      id: 2,
      ts: '2026-11-17T12:00:00Z',
      query_id: 'q2',
      connection_id: 'prod-sqlserver',
      engine: 'sqlserver',
      environment: 'prod',
      statement: null,
      rows: null,
      duration_ms: null,
      status: 'running',
      source: 'agent',
    },
  ],
  // The /status endpoint returns a count here; the page only consumes the
  // GET /connections list, which the stub blob shares under the same key.
  connections: [
    {
      id: 'prod-sqlserver',
      label: 'Prod SQL',
      engine: 'sqlserver',
      environment: 'prod',
      host: 'sql.prod.local',
      port: 1433,
      database: 'Db',
      secret_state: 'missing',
      enabled: 1,
    },
  ],
  last_seq: 5,
  counters: { running: 1, ok: 1, error: 0, total: 2 },
}

const CONNECTIONS = [
  {
    id: 'prod-sqlserver',
    label: 'Prod SQL',
    engine: 'sqlserver',
    environment: 'prod',
    host: 'sql.prod.local',
    port: 1433,
    database: 'Db',
    username: 'hermes_ro',
    secret_ref: 'DB_PROD_SQLSERVER_PASSWORD',
    secret_state: 'missing',
    read_only: 1,
    enabled: 1,
    options_json: '{}',
  },
]

const HEALTH = {
  engines: {
    sqlserver: { available: true, module: 'pymssql' },
    postgres: { available: true, module: 'psycopg' },
    redis: { available: false, module: 'redis', install: 'uv pip install redis>=5,<7' },
  },
}

function fakeCtx() {
  const ctx = {
    restCalls: [],
    registered: [],
    settingsPage: null,
    events: [],
    disposers: 0,
    rest(path, opts) {
      ctx.restCalls.push({ path, opts })
      return Promise.resolve({ ok: true })
    },
    register(contribution) {
      ctx.registered.push(contribution)
    },
    registerMany(contributions) {
      ctx.registered.push(...contributions)
    },
    registerSettingsPage(page) {
      ctx.settingsPage = page
    },
    onEvent(type, listener) {
      ctx.events.push({ type, listener })
      return () => {
        ctx.disposers += 1
      }
    },
    onDispose() {},
  }
  return ctx
}

function render(component, props = {}) {
  // Components are plain functions over stub hooks — call them directly.
  return component(props)
}

test('plugin default export e id', () => {
  assert.equal(plugin.id, 'database-connector')
  assert.equal(typeof plugin.register, 'function')
})

test('register: página, sidebar, paleta, chip, settings e os 4 listeners', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  const areas = Object.fromEntries(ctx.registered.map((c) => [c.area, c]))
  assert.equal(areas.routes.data.path, '/databases')
  assert.equal(typeof areas.routes.render, 'function')
  assert.deepEqual(
    { path: areas['sidebar.nav'].data.path, label: areas['sidebar.nav'].data.label },
    { path: '/databases', label: 'Bancos' },
  )
  assert.equal(areas['statusbar.right'].id, 'chip')
  assert.equal(areas.palette.data.label, 'Abrir Bancos')
  // paleta navega para a página
  areas.palette.data.run()
  assert.ok(host.navigations.includes('/databases'))
  // chip navega para a página
  assert.deepEqual(ctx.events.map((e) => e.type).sort(), [
    'plugin.database-connector.connection.changed',
    'plugin.database-connector.query.failed',
    'plugin.database-connector.query.finished',
    'plugin.database-connector.query.started',
  ])
  // settings page com sub-página drivers
  assert.equal(ctx.settingsPage.id, 'connections')
  assert.equal(ctx.settingsPage.children[0].id, 'drivers')
})

test('eventos invalidam a query key do plugin', () => {
  resetQueryCalls()
  const ctx = fakeCtx()
  plugin.register(ctx)
  for (const { listener } of ctx.events) listener({})
  const keys = queryClient.invalidations.map((inv) => inv.queryKey)
  assert.ok(keys.length >= 4)
  for (const key of keys) assert.deepEqual(key, ['database-connector'])
})

test('useQuery com polling de 5 s em status e queries', () => {
  resetQueryCalls()
  setStubData({ ...STUB_DATA })
  render(ActivityPage)
  render(StatusChip)
  const byKey = new Map(queryCalls.map((call) => [call.queryKey[1], call]))
  assert.equal(byKey.get('status').refetchInterval, 5000)
  assert.equal(byKey.get('queries').refetchInterval, 5000)
})

test('página de atividade: labels, statement truncado, sem segredo', () => {
  resetQueryCalls()
  setStubData({ ...STUB_DATA })
  const raw = text(render(ActivityPage))
  for (const label of ['Bancos', 'Consultas recentes', 'Hora', 'Conexão', 'Engine', 'Duração', 'Status', 'Último acesso', 'consulta ativa']) {
    assert.ok(raw.includes(label), 'falta label: ' + label)
  }
  // statement de 300 chars nunca aparece cru
  assert.ok(!raw.includes('S'.repeat(130)))
  // nenhum valor de segredo em lugar nenhum
  assert.ok(!raw.includes('DB_PROD_SQLSERVER_PASSWORD'))
  // estado por conexão (desfecho B: card último acesso) presente
  assert.ok(raw.includes('ociosa') || raw.includes('consulta ativa'))
})

test('chip: consultas ativas e bancos', () => {
  resetQueryCalls()
  setStubData({ ...STUB_DATA })
  assert.match(text(render(StatusChip)), /1 consulta · 1 banco/)
  // erro de backend → indicador de falha, não número congelado
  setStubData({}, { isError: true })
  assert.match(text(render(StatusChip)), /indisponível/)
})

test('settings: lista de conexões, secret_state e aviso de env var', () => {
  resetQueryCalls()
  setStubData({ connections: CONNECTIONS })
  const raw = text(render(ConnectionsManager))
  for (const label of ['Conexões', 'Nova conexão', 'Testar', 'Editar', 'Excluir', 'segredo: missing', 'Settings ▸ Environment']) {
    assert.ok(raw.includes(label), 'falta label: ' + label)
  }
})

test('settings: drivers com comando de install e aviso de gateway', () => {
  resetQueryCalls()
  setStubData({ ...HEALTH })
  const raw = text(render(DriversPage))
  assert.ok(raw.includes('reinicie o gateway'))
  assert.ok(raw.includes('uv pip install redis>=5,<7'))
  assert.ok(raw.includes('disponível'))
  assert.ok(raw.includes('faltando'))
})

test('form: campos por engine e badge de produção', () => {
  resetQueryCalls()
  setStubData({})
  const prod = text(render(ConnectionForm, {
    initial: { id: 'x', engine: 'sqlserver', environment: 'prod', host: 'h', database: 'd' },
    connections: [],
    onDone: () => {},
  }))
  assert.ok(prod.includes('produção — exige confirmação ao salvar'))
  assert.ok(prod.includes('secret_ref'))
  assert.ok(prod.includes('Driver ODBC'))
  const redis = text(render(ConnectionForm, {
    initial: { id: 'r', engine: 'redis', environment: 'local', host: 'h' },
    connections: [],
    onDone: () => {},
  }))
  assert.ok(redis.includes('db_index') && redis.includes('TLS'))
  // redis não tem campo database de nível superior
  assert.ok(!redis.includes('obrigatório (db/schema)'))
})

test('estilo e ciclo de vida: sem hex, sem window timers, sem innerHTML', () => {
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(SOURCE), 'cor hex hardcoded')
  assert.ok(!SOURCE.includes('setInterval'), 'timer fora do SDK')
  assert.ok(!SOURCE.includes('innerHTML'))
  assert.ok(!SOURCE.includes('dangerouslySetInnerHTML'))
  assert.ok(SOURCE.includes('var(--ui-'))
})
