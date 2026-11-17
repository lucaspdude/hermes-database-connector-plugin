# Spec Delta: connection-store

## Purpose

Persistência do estado do plugin `database-connector` em SQLite: conexões de banco (SQL Server, PostgreSQL, Redis) e log de consultas, com CRUD validado, segredo guardado apenas como referência a env var e resolução por perfil ativo. É a fonte única de verdade compartilhada pelo backend REST e pelas tools do agente.

## ADDED Requirements

### Requirement: Persistência em SQLite do plugin
O plugin SHALL persistir conexões e consultas num banco SQLite único em `<hermes home>/plugin-data/database-connector/` via `plugin_db` (WAL, `foreign_keys=True`), com schema criado por migração versionada. Os dados SHALL sobreviver a restart do Hermes.

#### Scenario: Conexão sobrevive a restart
- **WHEN** uma conexão é criada e o Hermes é reiniciado
- **THEN** a conexão continua listada com os mesmos campos e segue habilitada ou desabilitada como estava

#### Scenario: Escritas concorrentes
- **WHEN** a API REST e uma futura tool escrevem no banco ao mesmo tempo
- **THEN** nenhuma escrita corrompe o banco ou perde dados (busy timeout + transação curta); uma espera a outra

### Requirement: CRUD de conexões validado
O store SHALL manter conexões identificadas por `id` slug estável e SHALL aceitar apenas `engine` em {"sqlserver", "postgres", "redis"} e `environment` em {"prod", "homolog", "local"}. Upsert por `id` atualiza a conexão; exclusão SHALL apagar em cascata as consultas da conexão. Conexão nova ou alterada SHALL aplicar os defaults declarados (`read_only: true`, `enabled: true`, `options: {}`) sem default implícito de `environment`.

#### Scenario: Criar conexão válida
- **WHEN** uma conexão com `id`, `engine`, `environment`, `host` e campos válidos é salva
- **THEN** fica persistida com os defaults aplicados e `created_at`/`updated_at` em ISO-8601 UTC

#### Scenario: Engine ou ambiente inválido
- **WHEN** uma conexão é salva com `engine` ou `environment` fora dos enums aceitos
- **THEN** a escrita é rejeitada com erro claro, sem gravar a linha

#### Scenario: Exclusão em cascata
- **WHEN** uma conexão com consultas registradas é apagada
- **THEN** suas linhas do log de consultas são apagadas junto, e `GET /queries` não as retorna mais

### Requirement: Segredo apenas por referência
O store SHALL guardar apenas o **nome** da env var (`secret_ref`) e SHALL NEVER persistir o valor do segredo. A resolução do valor SHALL ocorrer em runtime via `agent.secret_scope.get_secret` no perfil ativo, classificando o estado como "resolved" (env existe), "missing" (env não existe) ou "unscoped" (multiplexação sem escopo — `UnscopedSecretError`).

#### Scenario: Valor de segredo nunca persistido
- **WHEN** uma conexão é salva com `secret_ref: "DB_PROD_SQLSERVER_PASSWORD"`
- **THEN** o banco contém apenas esse nome; nenhum campo guarda a senha, e logs não a contêm

#### Scenario: Segredo ausente no perfil
- **WHEN** a env var referenciada não existe no perfil ativo
- **THEN** o estado do segredo é "missing" e as operações que exigem conexão falham com erro claro, sem tentar outra fonte

#### Scenario: Multiplexação sem escopo
- **WHEN** o plugin roda em multiplexação sem escopo de segredo definido
- **THEN** a resolução falha com estado "unscoped" e erro claro, sem expor `os.environ`

### Requirement: Log de consultas consistente
O store SHALL registrar cada consulta num ciclo `query.started → query.finished | query.failed` com `query_id` único: linha inserida como "running" no início e atualizada para "ok" ou "error" ao terminar (com `rows`, `duration_ms`, `error` truncado quando houver). O statement registrado SHALL vir truncado (máx. 500 chars) e com literais mascarados, e o log SHALL NEVER conter senha, DSN completo, parâmetros, resultados ou header de conexão.

#### Scenario: Ciclo completo de consulta
- **WHEN** uma consulta é iniciada e termina com sucesso
- **THEN** existe uma única linha no log com status final "ok", duração e contagem de linhas — não duas

#### Scenario: Consulta que falha
- **WHEN** uma consulta falha ou excede timeout
- **THEN** a linha passa a status "error" com mensagem truncada e sem DSN/senha

#### Scenario: Statement redigido no log
- **WHEN** uma consulta com literais (`WHERE id = 42`) é registrada com o log de statement ligado
- **THEN** o statement é gravado truncado com literais mascarados (`WHERE id = ?`) e sem comentários

### Requirement: Retenção do log de consultas
O store SHALL limitar o `query_log` a 1.000 linhas: a cada escrita que exceder o limite, as linhas mais antigas SHALL ser apagadas na mesma transação da escrita, preservando sempre as consultas mais recentes.

#### Scenario: Poda ao exceder o cap
- **WHEN** uma nova consulta é registrada e o log já tem 1.000 linhas
- **THEN** a linha mais antiga é apagada na mesma transação e o total nunca excede 1.000 linhas
