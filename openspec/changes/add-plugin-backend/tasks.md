# Tasks: add-plugin-backend

Implementa as fases 0 e 1 do plano (`docs/plans/hermes-plugins/database-connector/`). Trabalhar em feature branch com PR para a `main` (AGENTS.md); nada é commitado direto na `main`.

## 1. Esqueleto do pacote

- [x] 1.1 Criar `plugin.yaml` **na raiz do repo** (o repo é o próprio pacote instalável — o install lê a árvore do clone) com `manifest_version: 2`, `python_dependencies` (`psycopg[binary]>=3.1,<4`, `redis>=5,<7`, `pymssql>=2.3,<3` — faixa superior obrigatória) e `provides_tools` vazio (preenchido só na change de tools); verificar que `hermes plugins validate` aceita o manifesto
- [x] 1.2 Criar `__init__.py` com `register(ctx)` no-op (sem registrar tool ainda) e verificar que o plugin carrega sem erro nos logs
- [x] 1.3 Criar `dashboard/manifest.json` (`{ "name": "database-connector", "api": "plugin_api.py" }`) e `dashboard/plugin_api.py` com `router = APIRouter()` e rota ping; verificar com `GET /api/plugins/database-connector/ping` no gateway local
- [ ] 1.4 Criar `desktop/plugin.js` mínimo (ESM, sem build, default export `{ id: 'database-connector', register(ctx) {} }`) e verificar que aparece em Capabilities → Plugins do Desktop
- [ ] 1.5 Habilitar no perfil (`hermes plugins enable database-connector`) e ligar o toggle do Desktop; verificar que `ctx.rest` alcança o mount com o plugin em `plugins.enabled`
- [ ] 1.6 Rodar `hermes plugins validate <dir> --install-deps` e `hermes plugins doctor <dir> --ci` e confirmar saída limpa; aceite da fase 0: plugin carregado nas duas metades
- [x] 1.7 Configurar pytest com fakes de `ctx` (store temporário) e o workflow de CI do repo (`.github/workflows/ci.yml`) rodando os testes em cada PR; verificar CI verde num PR de teste

## 2. Store SQLite e CRUD (service.py)

- [x] 2.1 Criar `service.py` no root do pacote com conexão via `plugin_db("database-connector", "connector.db")` (WAL, `foreign_keys=True`, `busy_timeout`) e migração versionada criando `connections` e `query_log` (+ índices `ix_query_log_ts`, `ix_query_log_conn`) conforme specs/connection-store; verificar com teste de unidade que a migração é idempotente e cria todos os campos/índices
- [x] 2.2 Implementar CRUD de conexões: upsert por `id`, validação de `engine` e `environment` (enums), defaults (`read_only: 1`, `enabled: 1`, `options_json: '{}'`), `created_at`/`updated_at` ISO-8601 UTC e delete em cascata do log; verificar com teste de unidade (criar → upsert → delete cascata) que linhas do log somem com a conexão
- [x] 2.3 Implementar `resolve_secret(connection)` via `agent.secret_scope.get_secret` retornando estado `resolved`/`missing`/`unscoped` (capitura de `UnscopedSecretError` em multiplexação sem escopo); verificar com teste de unidade cobrindo as três condições
- [x] 2.4 Garantir que nenhum caminho de escrita persiste valor de segredo (só `secret_ref`); verificar com teste de unidade que grep no arquivo do banco não encontra a senha de teste
- [x] 2.5 Implementar a poda do `query_log` com cap de 1.000 linhas na escrita (mesma transação); verificar com teste de unidade que inserir além do cap apaga as linhas mais antigas e o total nunca excede 1.000

## 3. Teste de conexão e health de drivers

- [x] 3.1 Implementar `test_connection()` por engine: abrir com os timeouts da conexão, probe mínimo (`SELECT 1` / `PING`), retornar `latency_ms` e `server_version` ou erro limpo; verificar com teste de unidade usando fake de driver (abre, sonda, fecha) e teste manual quando houver instância real — **probe real contra instância pendente (hostgator)**
- [x] 3.2 Implementar detecção de driver com `importlib.util.find_spec` (sem importar) por engine e mensagem de erro `driver_missing` com comando `install` (`uv pip install --python <sys.executable> <pkg>`); verificar forçando a ausência de um driver (ambiente sem ele) que só aquela engine reporta indisponível e as outras seguem funcionando — **probe real contra instância pendente (hostgator)**

## 4. Rotas REST e eventos

- [ ] 4.1 Implementar `GET/POST/PUT/DELETE /connections` com 201 em create, 409 em `id` duplicado, upsert em PUT e payload sem valor de segredo (`secret_ref` + `secret_state` apenas); verificar com `curl` via gateway local (criar, listar, atualizar, conflito)
- [ ] 4.2 Implementar `POST /connections/{id}/test` ligado ao `test_connection()`, com erro claro e sem stack trace quando o driver falta ou o banco não responde; verificar com `curl` contra conexão válida e contra conexão de host inexistente. Cada teste registra linha no `query_log` com `source: "ui"` e `statement` NULL; verificar a linha no log após o teste
- [ ] 4.3 Implementar `GET /queries` com `limit` (padrão 100), `connection_id` e `since_id`, ordem decrescente por tempo; verificar com `curl` após semear o `query_log` manualmente
- [ ] 4.4 Implementar `GET /status` devolvendo `{ active, connections, last_seq, counters }` com filtro opcional `?session_id=`; verificar inserindo uma linha "running" no log e conferindo que ela aparece em `active` e sai quando marcada "ok"
- [ ] 4.5 Implementar `GET /health` devolvendo por engine `{ available, module, install }`; verificar com `curl` num host completo (três `available: true`) e num ambiente sem um driver (uma `available: false` com comando pronto)
- [ ] 4.6 Emitir `plugin.database-connector.connection.changed` (`{ connection_id, action }`) em created/updated/deleted; verificar com um listener/observer do gateway durante um ciclo de CRUD
- [ ] 4.7 Padronizar erros 4xx como `{ error, detail }` em texto claro e acionável, sem stack trace, DSN ou segredo; verificar com `curl` de corpo inválido e de conexão inexistente

## 5. Validação da change

- [ ] 5.1 Re-rodar `hermes plugins validate <dir> --install-deps` e `hermes plugins doctor <dir> --ci` com o backend completo; confirmar saída limpa
- [ ] 5.2 Bateria manual de ponta a ponta da API no Hermes do host `hostgator` (via ssh): criar conexão → listar → testar contra as instâncias reais (SQL Server e Redis; Postgres local/Docker) → ver `/status` coerente → deletar e conferir cascata no log; registrar desvios no PR
- [ ] 5.3 Testar o escape de host problemático: `hermes plugins install --no-deps` (plugin instalado e desabilitado, `/health` orientando o comando por engine); verificar que `hermes pm repair` re-sincroniza as deps após instalação manual do driver
- [ ] 5.4 Abrir PR da feature branch para a `main` (Conventional Commits, CI verde, squash merge) com resumo dos aceites verificados
