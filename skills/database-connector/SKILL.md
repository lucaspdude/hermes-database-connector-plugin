---
name: database-connector
description: How to use the database-connector tools (db_list_connections, db_query, db_redis_command) — read-only policy, primary guarantee, error codes (read_only, timeout, not_found, driver_missing, secret_missing, db_error) and limits (max_rows 1000, Redis 64 KiB, timeouts 10s/30s). Use before querying any SQL Server, PostgreSQL or Redis connection in Hermes.
---

# database-connector — read-only access to SQL Server, PostgreSQL and Redis

Three tools, always used in the same order:

| Tool | When to use |
|---|---|
| `db_list_connections` | **Sempre primeiro.** Descobre o `connection_id`, a engine, o environment e se o driver está instalado (`driver_missing`). Filtro opcional por `environment` (`prod`, `homolog`, `local`). |
| `db_query` | Uma única consulta SQL de leitura numa conexão `postgres` ou `sqlserver`. |
| `db_redis_command` | Um comando de leitura do Redis (allowlist fechada) numa conexão `redis`. |

Nunca invente `connection_id`: chame `db_list_connections` e use o `id`
retornado. A lista nunca traz `secret_ref`, usuário ou senha.

## Read-only policy (defesa em profundidade)

1. **Garantia primária — credencial de banco somente-leitura.** Cada conexão
   precisa ser configurada com um usuário de papel somente-leitura no banco
   (`read_only`). Isso é pré-requisito de uso, não opcional.
2. **Segunda camada — filtro do plugin.** `db_query` aceita apenas `SELECT` ou
   CTE `WITH ... SELECT`, uma statement por chamada, sem `;` de lote e sem
   comentário (`--`, `/* */`); `db_redis_command` só aceita a allowlist. Essa
   camada **não pega todas** as formas de escrita — `WITH ... DELETE`,
   `SELECT ... INTO` e `EXEC` passam por ela.
3. **Terceira camada — a transação `READ ONLY` (PostgreSQL).** Toda consulta
   roda dentro de `SET TRANSACTION READ ONLY`: o **próprio banco** recusa a
   escrita que escapou do filtro e nada é aplicado (rollback ao final, sempre).
   No **SQL Server** essa declaração não existe em T-SQL (quebraria toda
   consulta com erro de sintaxe), então a garantia é a **credencial
   somente-leitura** — sempre a primária.

## Limites

- `max_rows`: padrão **200**, teto **1000** (valores maiores são reduzidos).
- Redis: o valor volta truncado em **64 KiB** (`truncated: true`).
- Timeout de conexão **10 s** e de statement **30 s** (padrões; configuráveis
  por conexão em `options.connect_timeout_s` / `options.statement_timeout_s`).
- **Recusa conservadora (limitação conhecida):** a detecção de lote e de
  comentário é textual, então statements com `;` ou `--` **dentro de um
  literal** (ex.: `WHERE nome = 'a;b'`) são recusadas com `code: "read_only"`
  mesmo sendo leitura pura. É um falso positivo seguro: reescreva a consulta
  (use parâmetro ou mude o literal).

## Códigos de erro — o que cada um significa e o que fazer

Toda falha responde `{ ok: false, error, code }`:

| `code` | Significado | Como agir |
|---|---|---|
| `read_only` | Statement/comando recusado pela política de leitura (escrita, DDL, `SET`/`EXEC`, lote com `;`, comentário) ou comando Redis fora da allowlist. Nada chegou ao servidor. | Reescreva como um único `SELECT` (ou CTE de leitura); no Redis, troque por um comando da allowlist (ex.: `SCAN` no lugar de `KEYS`). |
| `timeout` | A consulta excedeu o timeout da conexão ou da statement. | Reduza o escopo (filtro mais específico, menos linhas) ou peça para aumentar `statement_timeout_s` na conexão. |
| `not_found` | `connection_id` inexistente, desabilitado ou não informado. | Chame `db_list_connections` e use um `id` válido. |
| `driver_missing` | O driver da engine não está instalado neste host. | Rode o comando `install` que vem na resposta e **reinicie o gateway depois de instalar** (um processo vivo não reimporta o driver). |
| `secret_missing` | A env var do `secret_ref` da conexão não existe no perfil ativo. | Configure a env var da conexão; o erro nunca revela nomes de outras env vars. |
| `db_error` | O banco recusou ou falhou: erro de sintaxe, permissão, ou escrita barrada pela transação READ ONLY (ex.: `cannot execute DELETE in a read-only transaction`). | Leia `error`; para READ ONLY, reescreva a consulta como leitura pura. |

O handler **sempre** devolve uma string JSON e nunca levanta exceção — mesmo
falha interna vira `{ ok: false, code: "db_error" }` sem stack trace.

## Fluxo de exemplo

1. **Listar** — `db_list_connections` (ou com `environment: "prod"`):

```json
{ "ok": true, "count": 2, "connections": [
  { "id": "prod-sqlserver", "label": "Produção - SQL Server", "engine": "sqlserver",
    "environment": "prod", "host": "sql.prod.local", "port": 1433,
    "database": "HubFinanceira", "read_only": true },
  { "id": "cache-redis", "label": "Cache", "engine": "redis", "environment": "homolog",
    "host": "redis.internal", "port": 6379, "database": "2", "read_only": true }
]}
```

2. **Consultar** — `db_query` com o `id` escolhido:

```json
{ "connection_id": "prod-sqlserver",
  "statement": "SELECT TOP 10 id, status FROM pedidos WHERE status = 'aberto'",
  "max_rows": 10 }
```

Resposta: `{ "ok": true, "rows": [...], "row_count": 10, "truncated": false,
"duration_ms": 42, "connection": {...} }`.

E no Redis — `db_redis_command` com `{ "connection_id": "cache-redis",
"command": "HGETALL", "args": ["pedido:10"] }` → `{ "ok": true, "value": {...},
"duration_ms": 3 }`.

3. **Interpretar o erro** — se vier `{ "ok": false, "code": "read_only", "error":
"apenas leitura: a tool aceita uma unica statement SELECT (ou CTE WITH ...
SELECT) (recebido 'update')" }`, a consulta não foi executada: reescreva. Se
vier `db_error` com `cannot execute DELETE in a read-only transaction`, o banco
barrou uma escrita disfarçada — nada foi alterado, reescreva como leitura.
