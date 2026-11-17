# Spec Delta: backend-api

## Purpose

Contrato REST do plugin `database-connector` em `/api/plugins/database-connector/`: CRUD de conexões, teste de conexão, leitura do log de consultas, status consolidado para a UI e health de drivers. É a única via de acesso ao estado do plugin pelo Desktop — o estado canônico vem sempre de REST; eventos apenas invalidam.

## ADDED Requirements

### Requirement: CRUD de conexões via REST
O backend SHALL expor `GET /connections`, `POST /connections`, `PUT /connections/{id}` (update/upsert) e `DELETE /connections/{id}` em `/api/plugins/database-connector/`. `POST` SHALL retornar 201 com a conexão criada e 409 quando o `id` já existir. Nenhuma resposta SHALL conter valor de segredo — apenas `secret_ref` e `secret_state`.

#### Scenario: Criar conexão
- **WHEN** `POST /connections` recebe uma conexão válida com `id` novo
- **THEN** responde 201 com `{ ok, connection }` e a conexão aparece no próximo `GET /connections`

#### Scenario: Id duplicado
- **WHEN** `POST /connections` recebe um `id` que já existe
- **THEN** responde 409 com `{ error, detail }` sem criar nem alterar a conexão existente

#### Scenario: Resposta sem segredo
- **WHEN** qualquer rota devolve uma conexão com `secret_ref` configurado
- **THEN** o payload contém `secret_ref` e `secret_state` ("resolved" | "missing" | "unscoped") e nunca o valor do segredo

### Requirement: Teste de conexão
O backend SHALL expor `POST /connections/{id}/test` que abre a conexão real, executa um probe mínimo (`SELECT 1` em SQL Server/PostgreSQL, `PING` em Redis) e responde `{ ok, latency_ms, server_version? }` ou `{ ok: false, error }`. O teste SHALL respeitar os timeouts configurados na conexão e SHALL falhar com mensagem clara quando o driver da engine não estiver disponível no host. Cada teste SHALL registrar uma linha no `query_log` com `source: "ui"` e `statement` NULL, com `duration_ms` igual à latência e `status` "ok" ou "error".

#### Scenario: Teste bem-sucedido
- **WHEN** o banco alvo responde dentro do timeout
- **THEN** responde `{ ok: true, latency_ms, server_version }`

#### Scenario: Driver indisponível
- **WHEN** o módulo Python da engine não está instalado no host
- **THEN** responde `{ ok: false, error }` indicando a engine sem driver, sem stack trace

#### Scenario: Teste registrado no log
- **WHEN** um teste de conexão é executado pela rota de teste
- **THEN** uma linha com `source: "ui"` e `statement` NULL aparece no `query_log` com o resultado do teste

### Requirement: Leitura do log de consultas
O backend SHALL expor `GET /queries` com parâmetros `limit` (padrão 100), `connection_id` e `since_id`, retornando as consultas mais recentes primeiro (`{ queries: [...] }`).

#### Scenario: Listar consultas recentes
- **WHEN** `GET /queries?limit=100` é chamado após consultas registradas
- **THEN** retorna as consultas em ordem decrescente de tempo com metadados (conexão, engine, ambiente, duração, status, statement redigido) e sem senha, DSN, parâmetros ou resultado

### Requirement: Status consolidado
O backend SHALL expor `GET /status` retornando `{ active, connections, last_seq, counters }`, onde `active` são as consultas com status "running" e `last_seq` é o maior id do `query_log` (para reconciliação de eventos perdidos). Com `?session_id=`, SHALL filtrar as consultas ativas por sessão.

#### Scenario: Consulta ativa aparece no status
- **WHEN** existe uma linha "running" no `query_log`
- **THEN** `GET /status` a lista em `active` com conexão, engine e tempo de início

#### Scenario: Reconciliação após evento perdido
- **WHEN** a UI perde um evento `query.finished` e consulta `GET /status`
- **THEN** o estado devolvido reflete o `query_log` (a consulta já não está em `active`), tornando o chip correto após polling/reconciliação

### Requirement: Health de drivers por engine
O backend SHALL expor `GET /health` retornando, para cada engine (`sqlserver`, `postgres`, `redis`), `{ available, module, install }`, onde `install` é o comando pronto para instalar o driver no ambiente gerenciado (`uv pip install --python <sys.executable> <pkg>`, com o caminho do `uv` resolvido pelo plugin — glob `~/.hermes/tools/uv-*/uv` com fallback para `uv`, pois o binário pode não estar no PATH do host). A detecção SHALL ser feita sem importar o driver (import lazy): engine sem driver não pode derrubar as outras nem o carregamento do plugin.

#### Scenario: Host com todos os drivers
- **WHEN** `GET /health` é chamado num host com os três drivers instalados
- **THEN** cada engine devolve `available: true` com o nome do módulo

#### Scenario: Host sem um driver
- **WHEN** um driver falta no host
- **THEN** a engine correspondente devolve `available: false` e um comando `install` acionável, e as demais engines continuam `available: true`

### Requirement: Evento connection.changed
O backend SHALL emitir `plugin.database-connector.connection.changed` via `broadcast_plugin_event` após cada operação de CRUD, com payload `{ connection_id, action: "created" | "updated" | "deleted" }`.

#### Scenario: CRUD notifica a UI
- **WHEN** uma conexão é criada, atualizada ou apagada via REST
- **THEN** o evento `connection.changed` é emitido com o `connection_id` e a ação

### Requirement: Erros HTTP sem vazamento
Toda resposta de erro 4xx SHALL ter o formato `{ error, detail }`, em português claro e acionável, e SHALL NEVER incluir stack trace, DSN completo, senha ou valor de segredo.

#### Scenario: Erro de validação
- **WHEN** o corpo de `POST /connections` traz `engine` inválida
- **THEN** responde 4xx com `{ error, detail }` explicando os valores aceitos, sem stack trace
