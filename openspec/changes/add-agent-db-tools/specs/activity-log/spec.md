# Spec Delta: activity-log

## Purpose

Ciclo de vida de cada consulta registrada no log com status "running → ok/error" e os eventos `query.started` / `query.finished` / `query.failed` que alimentam a atividade ao vivo — com `query_id` único para reconciliação e statement redigido no log.

## ADDED Requirements

### Requirement: Ciclo de consulta com linha única
Toda consulta executada (SQL ou Redis) SHALL produzir uma única linha no `query_log` com `query_id` único: inserida como "running" antes da execução e atualizada para "ok" (com `rows`, `duration_ms`) ou "error" (com mensagem truncada) ao terminar — nunca duas linhas para o mesmo ciclo.

#### Scenario: Consulta bem-sucedida
- **WHEN** uma consulta é iniciada e conclui com sucesso
- **THEN** a linha passa de "running" para "ok" com contagem de linhas e duração, mantendo o mesmo `query_id`

#### Scenario: Consulta com erro ou timeout
- **WHEN** uma consulta falha, excede timeout ou é recusada pela política
- **THEN** a linha passa a "error" com o código/mensagem truncada e a duração até a falha

### Requirement: Eventos do ciclo de consulta
O plugin SHALL emitir `plugin.database-connector.query.started` antes de abrir a conexão e exatamente um de `query.finished` (sucesso) ou `query.failed` (erro) por `query_id`, via `broadcast_plugin_event`. Payloads SHALL conter `query_id`, `connection_id`, `label`, `engine`, `environment`, `database` (nome de exibição — nunca DSN), `session_id` (quando houver), timestamps e, no fim, `rows`/`duration_ms`/`status`/`error`. Os eventos SHALL ser tratados como sinal de invalidação: o estado canônico continua sendo `GET /queries` e `GET /status`.

#### Scenario: Eventos de um ciclo completo
- **WHEN** uma consulta é executada com sucesso
- **THEN** `query.started` e `query.finished` são emitidos com o mesmo `query_id` e o `database` é o nome de exibição do banco, sem DSN ou segredo

#### Scenario: Evento perdido é reconciliável
- **WHEN** um consumidor perde um `query.finished`
- **THEN** o estado no `GET /status` (linhas "running" + `last_seq`) permite reconstruir a verdade na próxima consulta ou no polling — o chip nunca fica torto permanentemente

### Requirement: Origem e sessão registradas
Cada linha de log SHALL registrar `source` ("agent" para tools, "ui" para execuções vindas da interface — incluindo os testes de conexão, que são logados com `statement` NULL) e `session_id`/`conversation_id` quando disponíveis no contexto da tool.

#### Scenario: Consulta vinda do agente
- **WHEN** uma tool executa uma consulta com `session_id` no contexto
- **THEN** o log registra `source: "agent"` e o `session_id`, permitindo recorte por chat no `GET /status?session_id=`

#### Scenario: Teste de conexão logado como ui
- **WHEN** um teste de conexão é executado pela rota de teste
- **THEN** o log registra uma linha com `source: "ui"` e `statement` NULL com o resultado do teste

### Requirement: Redaction do log
O statement registrado SHALL vir truncado em 500 caracteres, com literais numéricos e de string mascarados e sem comentários. O log SHALL NEVER conter senha, DSN completo, parâmetros de consulta, resultado ou header de conexão; a mensagem de erro SHALL vir truncada (500 chars) e com senha/host mascarados quando o driver os incluir. Com o toggle `log_statement` desligado, o log SHALL conter apenas metadados, sem texto de statement.

#### Scenario: Statement redigido
- **WHEN** uma consulta com literais e comentário é registrada com `log_statement` ligado
- **THEN** o log traz no máximo 500 caracteres com literais mascarados (`WHERE id = ?`) e sem o comentário

#### Scenario: Toggle desligado
- **WHEN** `log_statement` está desligado e uma consulta é executada
- **THEN** a linha do log não contém o texto do statement, apenas metadados

#### Scenario: Nenhum segredo no log
- **WHEN** qualquer consulta falha ou succeede em conexão com `secret_ref`
- **THEN** grep no banco e nos logs não encontra a senha nem o DSN completo
