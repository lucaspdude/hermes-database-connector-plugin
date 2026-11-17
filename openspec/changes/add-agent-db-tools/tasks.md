# Tasks: add-agent-db-tools

Implementa as fases 3 e 5 do plano. Pré-requisito: change `add-plugin-backend` implementada (store e rotas existindo). Feature branch + PR (AGENTS.md).

## 1. Drivers por engine

- [x] 1.1 Criar `drivers/__init__.py` com registry por engine e import lazy (`importlib.util.find_spec`); verificar com teste de unidade que engine sem driver é reportada como ausente sem quebrar o registro das outras — `uv run pytest -q` (tests/test_drivers_registry.py: import só na 1ª execução e cacheado por engine; `driver_missing` sem afetar as outras). **Driver real contra instância pendente (hostgator/bateria)**
- [x] 1.2 Implementar `drivers/postgres.py` (psycopg v3): conexão com `sslmode`/`connect_timeout_s`, execução com fetch limitado, transação `READ ONLY` e `statement_timeout`; verificar com teste de unidade de fake de conexão (abre, executa, faz rollback/close) e teste manual contra Postgres local quando disponível — testes unitários com fake em `tests/test_drivers_pg.py` (READ ONLY antes da statement, timeout repassado, `max_rows` com `truncated`, rollback/close com erro, shape uniforme com o SQL Server). **Driver real contra instância pendente (hostgator/bateria)**
- [x] 1.3 Implementar `drivers/sqlserver.py`: caminho padrão pymssql (timeouts de login e statement, `tds_version`, `trust_server_certificate`) e caminho opcional pyodbc detectado por `find_spec` (confirmado para o MVP; o teste manual do caminho pyodbc exige o MS ODBC Driver 18 no host — pular se ausente); verificar com teste de unidade de fake e teste manual contra a instância real de SQL Server (host `hostgator`) — fakes em `tests/test_drivers_mssql.py` (timeouts/tds no pymssql, pyodbc ausente → pymssql, pyodbc presente → string ODBC com `TrustServerCertificate=yes`). **Driver real contra instância pendente (hostgator/bateria)**
- [ ] 1.4 Implementar `drivers/redis.py` (redis-py): conexão com `db_index`/`tls`/timeouts, execução de comando com truncamento do valor em 64 KiB; verificar com teste de unidade de fake e teste manual contra Redis local
- [x] 1.5 Implementar `SET TRANSACTION READ ONLY` no caminho SQL Server (transação aberta na conexão antes da execução e revertida ao final); verificar com teste de unidade de fake que a transação READ ONLY envolve a execução — `tests/test_drivers_mssql.py` (READ ONLY emitido antes da statement; rollback contado mesmo quando a execução lança). **Driver real contra instância pendente (hostgator/bateria)**: confirmação de que o SQL Server aceita `SET TRANSACTION READ ONLY` ainda não feita

## 2. Ciclo de consulta, log e eventos

- [ ] 2.1 Implementar `service.run_query(connection, statement|command, max_rows, source, **kwargs)`: insere linha "running" → executa via driver → atualiza para "ok"/"error" → emite `query.started` e `query.finished`/`query.failed` com `query_id` único; verificar com teste de unidade (fake de driver) que existe uma única linha por ciclo com status final e payloads contendo `connection_id`, `engine`, `environment`, `database` de exibição e timestamps
- [ ] 2.2 Implementar o mapping driver → `code` estável (`timeout`, `db_error`, `driver_missing`, `secret_missing`, `not_found`, `read_only`) com truncamento de mensagem e máscara de senha/host; verificar com teste de unidade para cada mapeamento
- [ ] 2.3 Aplicar redaction dentro do ciclo: statement ≤ 500 chars com literais mascarados e sem comentário, erro truncado sem DSN/senha, toggle `log_statement` declarado no `config_schema` do `plugin.yaml` e lido com `ctx.get_config("log_statement", true)`; verificar com testes de unidade (statement com literais e comentário; toggle desligado) e grep no banco sem senha nem literal
- [ ] 2.4 Verificar com `curl` que `GET /queries` e `GET /status` (`active` + `last_seq`) refletem consultas reais, incluindo recorte por `?session_id=`

## 3. Tools do agente

- [ ] 3.1 Criar wrapper de registro à prova de exceção (handler sempre devolve JSON string, nunca levanta) e registrar as tools com `ctx.register_tool` + `check_fn` de "needs configuration"; verificar com teste de unidade que um driver que lança exceção vira `{ ok: false, code: "db_error" }` em JSON string sem stack trace
- [ ] 3.2 Implementar `db_list_connections` (filtro `environment`, sem `secret_ref`, marcação `driver_missing`); verificar com teste de unidade cobrindo lista completa, filtro e engine sem driver
- [ ] 3.3 Implementar `db_query` com a política read-only (verbos de escrita/DDL/sessão, rejeição de `;` e comentários, `max_rows` padrão 200/teto 1000) e códigos estáveis; verificar com testes de unidade: `SELECT` ok, `UPDATE` → `read_only`, lote → `read_only`, `not_found`, `secret_missing`, `driver_missing` com `install` + nota de reinício, timeout → `timeout`
- [ ] 3.4 Implementar `db_redis_command` com a allowlist fechada (`GET`, `MGET`, `STRLEN`, `EXISTS`, `TYPE`, `HGET`, `HMGET`, `HGETALL`, `HKEYS`, `HVALS`, `HLEN`, `LRANGE`, `LLEN`, `LINDEX`, `SMEMBERS`, `SCARD`, `SISMEMBER`, `ZRANGE`, `ZCARD`, `ZSCORE`, `SCAN`, `TTL`, `PTTL`, `DBSIZE`, `INFO`) e truncamento 64 KiB; fora da lista — `KEYS`, `MEMORY`, `OBJECT`, `CLIENT`, `CONFIG`, `FLUSH*`, `AUTH`, `SCRIPT`, `EVAL`, `DEBUG`, `SHUTDOWN`, `CLUSTER`, `REPLICAOF` — recusa sem contato com o servidor; verificar com testes de unidade: `GET`/`HGETALL`/`SCAN` ok, `FLUSHALL`/`EVAL`/`KEYS` → `read_only`
- [ ] 3.5 Atualizar `provides_tools` do manifesto para bater **exatamente** com as três tools registradas; verificar com `hermes plugins validate <dir> --install-deps` limpo
- [ ] 3.6 Verificar transação READ ONLY no Postgres **e no SQL Server** com caso que o filtro não pega: `WITH ... DELETE` retorna erro do banco sem efeito nas duas engines; verificar recusa de `SET` e `EXEC` com `code: "read_only"`

## 4. Skill e aceite

- [ ] 4.1 Criar `skills/database-connector/SKILL.md` explicando quando usar cada tool, a política read-only e o significado dos `code` de erro; verificar que a skill aparece no agente e orienta corretamente uma consulta de exemplo
- [ ] 4.2 Aceite do plano no chat: `db_list_connections` lista, `db_query` retorna linhas e `db_redis_command` devolve valor — rodado contra instâncias reais de SQL Server e Redis (no Hermes do host `hostgator`) e Postgres local/Docker — com o log/eventos visíveis em `GET /queries` e `GET /status` (a tela do Desktop vem na change seguinte)
- [ ] 4.3 Abrir PR da feature branch para a `main` (Conventional Commits, CI verde, squash merge) com resumo dos aceites verificados
