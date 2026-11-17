# Spec Delta: database-tools

## Purpose

Tools do agente para listar conexões e executar leituras (SQL e Redis) com contratos de entrada/saída estáveis, códigos de erro previsíveis e degradação por engine quando o driver não está no host.

## ADDED Requirements

### Requirement: Tool db_list_connections
O agente SHALL dispor de `db_list_connections` que lista as conexões habilitadas (`{ connections: [{ id, label, engine, environment, host, port, database, read_only, driver_missing? }] }`), com filtro opcional `environment` (prod/homolog/local). A resposta SHALL NEVER incluir `secret_ref` ou segredo. Conexão cuja engine não tem driver no host SHALL aparecer marcada com `driver_missing` (ou o comando de instalação), para o modelo não tentar executar e falhar.

#### Scenario: Listar antes de consultar
- **WHEN** o agente chama `db_list_connections` sem filtro com três conexões configuradas
- **THEN** recebe as três com metadados de conexão e nenhum segredo

#### Scenario: Filtro por ambiente
- **WHEN** o agente chama com `environment: "prod"`
- **THEN** só conexões de produção são retornadas

#### Scenario: Engine sem driver marcada
- **WHEN** o driver SQL Server não está instalado no host e existe conexão dessa engine
- **THEN** a conexão aparece na lista marcada como `driver_missing` e as outras engines seguem normais

### Requirement: Tool db_query
O agente SHALL dispor de `db_query` para executar **uma única statement SQL de leitura** numa conexão (`connection_id`, `statement`, `max_rows` opcional — padrão 200, teto 1000). Sucesso responde `{ ok: true, rows, row_count, truncated, duration_ms, connection }`; falha responde `{ ok: false, error, code }` com código estável entre `read_only`, `timeout`, `not_found`, `driver_missing`, `secret_missing`, `db_error`. `driver_missing` SHALL incluir o comando `install` pronto e a nota para reiniciar o gateway.

#### Scenario: Consulta SQL bem-sucedida
- **WHEN** `db_query` executa um `SELECT` válido numa conexão PostgreSQL
- **THEN** responde com as linhas (até `max_rows`), `row_count`, `truncated` e `duration_ms`

#### Scenario: Conexão inexistente
- **WHEN** `connection_id` não corresponde a nenhuma conexão
- **THEN** responde `{ ok: false, code: "not_found" }` sem tentar conectar

#### Scenario: Segredo ausente
- **WHEN** a env var do `secret_ref` não existe no perfil ativo
- **THEN** responde `{ ok: false, code: "secret_missing" }` orientando a configuração, sem vazar nomes de outras env vars

#### Scenario: Driver ausente
- **WHEN** o driver da engine não está instalado
- **THEN** responde `{ ok: false, code: "driver_missing", install: "<caminho do uv> pip install --python <sys.executable> <pkg>", note: "reinicie o gateway depois de instalar" }` sem derrubar as outras engines

### Requirement: Tool db_redis_command
O agente SHALL dispor de `db_redis_command` (`connection_id`, `command`, `args[]` opcionais) para comandos Redis de leitura. Sucesso responde `{ ok: true, value, duration_ms }` com o valor truncado em 64 KiB; comando fora da allowlist responde `{ ok: false, code: "read_only" }`.

#### Scenario: Comando de leitura
- **WHEN** o agente chama com `command: "HGETALL"` e uma chave
- **THEN** recebe o valor do hash com duração, truncado se exceder 64 KiB

#### Scenario: Comando bloqueado
- **WHEN** o agente chama com `command: "CONFIG"`
- **THEN** recebe `{ ok: false, code: "read_only" }` sem contato com o servidor

### Requirement: Handler estável e manifesto coerente
Todo handler de tool SHALL devolver **sempre uma JSON string** e SHALL NEVER levantar exceção — qualquer falha interna vira `{ ok: false, code, error }`. O `provides_tools` do manifesto SHALL listar exatamente as tools registradas. O registro SHALL usar `check_fn` que sinaliza ausência de conexão configurada (UX de "needs configuration") em vez de falha críptica.

#### Scenario: Falha interna não explode
- **WHEN** o driver lança uma exceção inesperada durante a execução
- **THEN** o agente recebe JSON string `{ ok: false, code: "db_error", error }` truncada e sem stack trace — nunca um erro não tratado

#### Scenario: Sem conexão configurada
- **WHEN** nenhuma conexão existe e o agente chama qualquer tool
- **THEN** a resposta orienta criar conexão pela tela de config (estado "needs configuration") em vez de erro críptico
