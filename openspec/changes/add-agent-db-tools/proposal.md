# Proposal: add-agent-db-tools

## Why

O backend do plugin (`add-plugin-backend`) já persiste conexões e expõe REST, mas o agente ainda não consegue consultar bancos: faltam os drivers por engine, as tools (`db_list_connections`, `db_query`, `db_redis_command`) e o ciclo de consulta com log e eventos — que é o que alimenta a atividade ao vivo exigida pelo usuário. Esta é a **change 2 de 3** do plano (`docs/plans/hermes-plugins/database-connector/`), cobrindo as fases 3 e 5 (tools + endurecimento).

## What Changes

- `drivers/` (`sqlserver.py` com pymssql e pyodbc opcional, `postgres.py` com psycopg v3, `redis.py` com redis-py): abrir, executar e fechar com timeouts, registro por engine e import lazy.
- `service.py`: `run_query()` com ciclo completo — insere linha "running" no `query_log`, executa, atualiza para "ok"/"error" e emite `query.started` / `query.finished` / `query.failed` via `broadcast_plugin_event`.
- Tools do agente registradas com schemas JSON estáveis, handler que **sempre** devolve JSON string e **nunca** levanta exceção, e `check_fn` que indica "precisa configurar conexão"; `provides_tools` do manifesto passa a bater exatamente com as registradas.
- Política read-only em defesa em profundidade: rejeição de statement de escrita, rejeição de lote (`;`) e comentários `--`/`/*`, transação `READ ONLY` no PostgreSQL, timeouts de statement, allowlist de comandos Redis de leitura; a garantia primária documentada é a credencial de banco somente-leitura (o filtro é a segunda camada).
- Limites e redaction: `max_rows` (padrão 200, teto 1000), valor Redis truncado em 64 KiB, statement no log truncado (500 chars) com literais mascarados, erros com `code` estável sem stack trace/DSN/senha.
- Skill do plugin (`skills/database-connector/SKILL.md`) orientando quando usar cada tool e a política read-only.

## Capabilities

### New Capabilities

- `database-tools`: as três tools do agente (listar conexões, consulta SQL de leitura, comando Redis de leitura), seus contratos de entrada/saída, códigos de erro estáveis e degradação por driver ausente.
- `query-policy`: garantia de somente-leitura em defesa em profundidade (filtro de statement, lote/comentário, transação READ ONLY, allowlist Redis), timeouts e limites de payload.
- `activity-log`: ciclo de consulta com log "running → ok/error" reconciliável e eventos `query.started` / `query.finished` / `query.failed`, com statement redigido no log.

### Modified Capabilities

(nenhuma — as capabilities de `add-plugin-backend` não têm requisitos alterados; o schema do store já as cobre)

## Impact

- **Manifesto**: `provides_tools` passa a declarar exatamente as três tools registradas.
- **Store**: `query_log` ganha produtores (as tools); nenhuma migração nova é esperada — o schema já existe desde `add-plugin-backend`.
- **Segurança**: leitura apenas; recusa de escrita/lote; segredo resolvido em runtime nunca aparece em log nem retorno; comandos Redis administrativos bloqueados.
- **Consumidores**: a change `add-desktop-live-ui` depende dos eventos e do log criados aqui; `GET /status` e `GET /queries` passam a ter conteúdo real.
- **Aceite do plano**: no chat, listar/consultar funciona e a atividade aparece em tempo real; `UPDATE`/`DELETE`/`SET` falham com `code: "read_only"`; grep no log não encontra senha nem literal do statement.
