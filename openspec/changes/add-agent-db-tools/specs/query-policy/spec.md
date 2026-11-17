# Spec Delta: query-policy

## Purpose

Garantir que o plugin execute **apenas leitura** em defesa em profundidade — filtro de statement na tool, rejeição de lote/comentário, transação READ ONLY no PostgreSQL e allowlist Redis — com timeouts e limites de payload, documentando a credencial de banco somente-leitura como garantia primária.

## ADDED Requirements

### Requirement: Recusa de statement de escrita
A tool SQL SHALL recusar com `code: "read_only"` qualquer statement que não seja leitura (verbos de escrita, DDL e comandos de sessão/transação), aceitando apenas `SELECT` e CTEs de leitura.

#### Scenario: Escrita recusada
- **WHEN** a statement começa com `UPDATE`, `DELETE`, `INSERT`, `MERGE` ou DDL (`CREATE`/`ALTER`/`DROP`/`TRUNCATE`)
- **THEN** a consulta não chega ao banco e a tool responde `{ ok: false, code: "read_only" }`

#### Scenario: Leitura aceita
- **WHEN** a statement é um `SELECT` ou CTE (`WITH ... SELECT`) válido e único
- **THEN** a consulta executa normalmente e retorna linhas

### Requirement: Statement única, sem lote nem comentário
A tool SHALL recusar statement contendo `;` seguido de outra statement e comentários `--` ou `/*`, porque o driver executa lote.

#### Scenario: Lote recusado
- **WHEN** a statement contém `; ` separando dois comandos
- **THEN** responde `{ ok: false, code: "read_only" }` sem executar nada

#### Scenario: Comentário recusado
- **WHEN** a statement contém `--` ou `/* */`
- **THEN** responde `{ ok: false, code: "read_only" }` sem executar nada

### Requirement: Transação READ ONLY no PostgreSQL e SQL Server
Consultas PostgreSQL SHALL executar dentro de transação `READ ONLY` com `statement_timeout` aplicado, e consultas SQL Server SHALL executar com `SET TRANSACTION READ ONLY` na conexão (caminho pymssql), de modo que formas que o filtro de palavra-chave não captura (`WITH ... DELETE`, `SELECT ... INTO`, funções com efeito colateral) sejam barradas pelo próprio banco nas duas engines.

#### Scenario: Escrita disfarçada barrada pelo PostgreSQL
- **WHEN** uma statement `WITH ... DELETE` passa pelo filtro e chega ao PostgreSQL
- **THEN** a transação READ ONLY faz o banco recusar a operação, e a tool devolve erro sem efeito no banco

#### Scenario: Escrita disfarçada barrada pelo SQL Server
- **WHEN** uma statement `WITH ... DELETE` passa pelo filtro e chega ao SQL Server
- **THEN** a transação READ ONLY faz o banco recusar a operação, e a tool devolve erro sem efeito no banco

### Requirement: Timeouts e limites
Toda conexão SHALL aplicar timeout de conexão (padrão 10 s) e de statement (padrão 30 s), configuráveis por conexão; `max_rows` SHALL ter padrão 200 e teto 1000; o valor retornado por comando Redis SHALL vir truncado em 64 KiB. Timeout SHALL mapear para `code: "timeout"` sem deixar linha "running" órfã.

#### Scenario: Statement lenta interrompida
- **WHEN** uma consulta excede o statement_timeout configurado
- **THEN** é interrompida, responde `{ ok: false, code: "timeout" }` e a linha do log termina em "error"

#### Scenario: Teto de linhas
- **WHEN** uma consulta retorna mais linhas que `max_rows`
- **THEN** a resposta traz até `max_rows` linhas com `truncated: true` e o log registra as linhas realmente entregues

### Requirement: Allowlist de comandos Redis
O comando Redis SHALL ser aceito apenas se estiver na allowlist fechada de leitura: `GET`, `MGET`, `STRLEN`, `EXISTS`, `TYPE`, `HGET`, `HMGET`, `HGETALL`, `HKEYS`, `HVALS`, `HLEN`, `LRANGE`, `LLEN`, `LINDEX`, `SMEMBERS`, `SCARD`, `SISMEMBER`, `ZRANGE`, `ZCARD`, `ZSCORE`, `SCAN`, `TTL`, `PTTL`, `DBSIZE`, `INFO`. Comandos fora da lista — incluindo os caros (`KEYS`, `MEMORY`, `OBJECT`, `CLIENT`) e os de escrita/administração (`CONFIG`, `FLUSH*`, `AUTH`, `SCRIPT`, `EVAL`, `DEBUG`, `SHUTDOWN`, `CLUSTER`, `REPLICAOF`) — SHALL ser recusados com `code: "read_only"` sem chegar ao servidor.

#### Scenario: Leitura aceita
- **WHEN** o comando é `GET` com uma chave
- **THEN** executa e devolve `{ ok: true, value, duration_ms }`

#### Scenario: Administração recusada
- **WHEN** o comando é `FLUSHALL` ou `EVAL`
- **THEN** responde `{ ok: false, code: "read_only" }` sem enviar nada ao Redis

#### Scenario: Comando caro recusado
- **WHEN** o comando é `KEYS` com um padrão
- **THEN** responde `{ ok: false, code: "read_only" }` sem enviar nada ao Redis

### Requirement: Garantia primária documentada
O plugin SHALL documentar como pré-requisito de uso a credencial de banco com papel somente-leitura, deixando explícito que o filtro do plugin é a segunda camada e não captura todas as formas de escrita (`WITH ... DELETE`, `SELECT ... INTO`, `EXEC`).

#### Scenario: README registra o pré-requisito
- **WHEN** o README do plugin descreve a configuração de conexões
- **THEN** inclui o pré-requisito do usuário somente-leitura e a limitação do filtro como segunda camada
