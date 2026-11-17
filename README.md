# hermes-database-connector-plugin

Agente lendo SQL Server, PostgreSQL e Redis com segurança: três tools
(`db_list_connections`, `db_query`, `db_redis_command`), log de atividade e
eventos invalidação.
(O README completo — instalação, configuração, rotas — vem na change seguinte;
aqui fica a política exigida pela spec `query-policy`.)

## Política read-only

**Pré-requisito de uso: credencial de banco com papel somente-leitura.** Toda
conexão precisa ser configurada com um usuário de leitura no banco
(`read_only`). Essa é a garantia **primária** — é ela que impede escrita, não o
plugin.

O filtro do plugin é a **segunda camada**, e não é exaustivo: `db_query` aceita
apenas uma statement `SELECT` (ou CTE `WITH ... SELECT`) por chamada, sem lote
(`;`) e sem comentário (`--`, `/* */`); `db_redis_command` só executa os
comandos da allowlist fechada. Formas de escrita que o filtro **não** pega:

- `WITH ... DELETE` (e qualquer CTE que termine em escrita);
- `SELECT ... INTO`;
- `EXEC` / `EXECUTE` (recusados pelo filtro quando são o lead da statement);

no PostgreSQL essas formas são barradas pela transação `SET TRANSACTION READ
ONLY` que o driver declara antes da consulta; no SQL Server **não existe** essa
declaração em T-SQL, então quem garante é a credencial somente-leitura.

### Limites

- `max_rows`: padrão **200**, teto **1000** (valores maiores são reduzidos).
- Redis: valor truncado em **64 KiB** (`truncated: true`).
- Timeouts: conexão **10 s** e statement **30 s** (configuráveis por conexão).
- **Limitação conhecida (recusa conservadora):** a detecção de lote/comentário é
  textual, então uma statement com `;` ou `--` **dentro de um literal** (ex.:
  `SELECT * FROM t WHERE nome = 'a;b'`) é recusada com `code: "read_only"` mesmo
  sendo leitura. É um falso positivo seguro: reescreva a consulta (parâmetro ou
  outro literal) quando isso acontecer.
