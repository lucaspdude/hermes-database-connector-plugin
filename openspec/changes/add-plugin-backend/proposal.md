# Proposal: add-plugin-backend

## Why

O plugin `database-connector` (SQL Server + PostgreSQL + Redis no Hermes Desktop) não existe ainda: o repo está vazio. Antes de tools do agente e telas do Desktop, o plugin precisa de uma **fonte única de verdade** — o pacote instalável e o backend que persiste conexões e log de consultas em SQLite e expõe REST. Sem essa base, as duas outras metades (tools e UI) não têm estado nem contratos.

Esta é a **change 1 de 3** que implementa o plano completo (`docs/plans/hermes-plugins/database-connector/`), cobrindo as fases 0 e 1 do plano. As changes seguintes: `add-agent-db-tools` (fases 3 e 5) e `add-desktop-live-ui` (fases 0.9, 2, 4 e 6).

## What Changes

- Esqueleto do pacote `~/.hermes/plugins/database-connector/`: `plugin.yaml` (manifest_version 2, `python_dependencies` psycopg/redis/pymssql, `provides_tools` vazio), `__init__.py` com `register(ctx)` no-op, `dashboard/manifest.json` + `dashboard/plugin_api.py` com router ping, `desktop/plugin.js` mínimo.
- `service.py` no root do pacote (fonte única, importado por tools e rotas): store SQLite via `plugin_db` (WAL), migração versionada com as tabelas `connections` e `query_log`, CRUD de conexões com cascata no log, resolução de segredo por referência (`secret_ref` → env var via `agent.secret_scope.get_secret`), `test_connection()` por engine, detecção de drivers por `importlib.util.find_spec`.
- Rotas REST em `/api/plugins/database-connector/`: `GET/POST/PUT/DELETE /connections`, `POST /connections/{id}/test`, `GET /queries`, `GET /status`, `GET /health`.
- Evento `connection.changed` emitido no CRUD (`broadcast_plugin_event`).
- `GET /health` como fonte de verdade da disponibilidade de drivers por engine (`{ available, module, install }`), para degradação sem quebrar as outras engines.

## Capabilities

### New Capabilities

- `connection-store`: persistência das conexões e do log de consultas em SQLite (`connections`, `query_log`), CRUD validado, segredo só por referência de env var, resolução por perfil ativo.
- `backend-api`: rotas REST do plugin (CRUD de conexões, teste, leitura do log, status consolidado, health de drivers), erros HTTP sem stack trace/DSN e evento `connection.changed`.

### Modified Capabilities

(nenhuma — repo greenfield, sem specs existentes)

## Impact

- **Novo pacote** `database-connector` (plugin Hermes: meia Python + meia desktop); nenhum código existente é alterado.
- **Dependências Python declaradas** no manifesto (`psycopg[binary]>=3.1,<4`, `redis>=5,<7`, `pymssql>=2.3,<3`) — instaladas pelo `hermes plugins install` com consentimento; o plugin nunca instala pacote em runtime (scanner de segurança bloqueia `pip install`).
- **Contratos consumidos**: `plugin_data_dir`/`plugin_db`, `agent.secret_scope.get_secret`, `broadcast_plugin_event`, mount `/api/plugins/<id>/` (verificados no código Hermes v0.23.1 pela pesquisa base do plano).
- **Validação**: `hermes plugins validate --install-deps` e `hermes plugins doctor --ci` limpos; teste do modo degradado (`--no-deps`) e do escape para host sem rede.
- As rotas `GET /queries` e `GET /status` retornam vazio nesta fase; ganham conteúdo quando `add-agent-db-tools` executar consultas e quando a UI consumir o status.
