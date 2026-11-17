# Design: add-agent-db-tools

## Context

O backend de `add-plugin-backend` já entrega o store (tabelas `connections`/`query_log`), `service.py` como fonte única e as rotas REST. Os drivers Python chegam pelo `hermes plugins install` (`python_dependencies` no manifesto — já declarados). Esta change conecta o agente a esse estado: drivers, ciclo de consulta e as três tools, com a política read-only da fase 5 do plano.

## Goals / Non-Goals

**Goals:**

- O agente lista conexões e executa leituras nas três engines com contratos estáveis e erros previsíveis (`code`).
- Cada consulta produz log "running → ok/error" e eventos `query.*` — a matéria-prima da atividade ao vivo da change seguinte.
- Leitura garantida em defesa em profundidade; degradação por engine sem derrubar as outras.

**Non-Goals:**

- Qualquer tela do Desktop (change `add-desktop-live-ui`); o aceite visual usa `GET /queries`/`GET /status` via curl.
- Escrita em banco, pool de conexões, parametrização de queries (placeholders do usuário), parser SQL completo.
- Novas migrações de schema — o schema de `add-plugin-backend` já comporta este ciclo.

## Decisions

1. **Registry de drivers com import lazy** — `drivers/__init__.py` resolve a engine por `importlib.util.find_spec` e só importa o módulo na execução. Alternativa descartada: import no topo do `service.py` — um driver ausente derrubaria o carregamento de todas as engines (R1).
2. **Um único ciclo em `service.run_query()`** — insere "running", executa via driver, atualiza fim, emite os três eventos; SQL e Redis usam o mesmo ciclo (Redis leva comando/args no lugar da statement). Alternativa descartada: ciclo separado por engine — duplicaria log/eventos e corria o risco de linhas "running" órfãs em só um dos caminhos.
3. **Wrapper de handler à prova de exceção** — cada tool é registrada através de um wrapper que captura qualquer exceção e devolve JSON string `{ ok: false, code, error }` (truncada, sem stack trace). Contrato do SDK: handler nunca levanta; alternativas (deixar propagar) quebram o agente.
4. **Política read-only em camadas** — (a) filtro na tool: verbos de escrita/DDL/sessão, rejeição de lote (`;`) e comentários; (b) PostgreSQL: transação `READ ONLY` + `statement_timeout` — pega o que o filtro não vê (`WITH ... DELETE`, `SELECT ... INTO`); (c) SQL Server: timeout de login/statement (pymssql) com usuário RO como garantia final; (d) Redis: allowlist explícita de leitura, recusa sem contato com o servidor. A garantia primária (credencial somente-leitura) é documentação/pré-requisito, não código — filtro por palavra-chave não é garantia (R10).
5. **pymssql como caminho padrão; pyodbc opcional** — detectado por `find_spec`; se presente, usa TLS nativo melhor (limitação do FreeTDS embutido com certificado self-signed). Ambos os caminhos expõem a mesma interface do driver ao service.
6. **Erros estáveis por código** — mapping único driver → `code` (`timeout`, `db_error`, `driver_missing`, `secret_missing`, `not_found`, `read_only`), aplicado nas tools e nas rotas; `driver_missing` carrega `install` pronto + nota de reinício do gateway.
7. **`check_fn` de configuração** — registro das tools com check que devolve o sinal de "needs configuration" quando não há conexão habilitada, dando UX correta em vez de erro críptico.
8. **Redaction no ponto único** — a redaction (500 chars, literais mascarados, sem comentário; erro sem senha/DSN) acontece dentro do ciclo `run_query`, único lugar que escreve statement/erro no log; o toggle `log_statement` é declarado no `config_schema` do manifesto e lido com `ctx.get_config("log_statement", true)` — mecanismo nativo do Hermes (`plugins.entries.<id>.settings`, editável no Desktop), avaliado com System One (confiança 1.0), sem rota, tabela ou UI própria.
9. **READ ONLY só no PostgreSQL** — a simetria com o SQL Server foi tentada e derrubada por fato técnico descoberto na revisão: `SET TRANSACTION` em T-SQL aceita apenas `ISOLATION LEVEL`, e a declaração causaria erro de sintaxe em 100% das consultas da engine. Garantias do SQL Server: filtro da tool, timeouts de login/statement e credencial somente-leitura como pré-requisito documentado; o driver nunca emite a declaração (teste negativo).
10. **Allowlist Redis fechada** — 25 comandos de leitura curados; fora ficam os caros (`KEYS`, `MEMORY`, `OBJECT`, `CLIENT`) e os de escrita/administração, recusados sem contato com o servidor.
11. **Testes de conexão logados** — `test_connection` registra linha no `query_log` (`source: "ui"`, `statement` NULL); removível depois se virar ruído.
12. **pyodbc confirmado para o MVP** — caminho detectado por `find_spec`; o teste manual exige o MS ODBC Driver 18 no host (`hostgator`).

## Risks / Trade-offs

- [Filtro por palavra-chave não é garantia] → transação READ ONLY (Postgres) + timeouts (SQL Server) + credencial RO documentada como pré-requisito de uso.
- [Multi-statement no SQL Server executa lote] → rejeição de `;` + comentários antes de enviar ao driver (R4).
- [Evento perdido deixa consumidor desatualizado] → eventos são invalidação; `GET /status` (`active` + `last_seq`) reconcilia — contrato já previsto no backend-api.
- [PII em statement logada] → redaction + truncamento + toggle `log_statement` (R8).
- [Driver instalado depois não entra no processo vivo] → `driver_missing` orienta reiniciar o gateway (R1b).

## Migration Plan

Sem migração: o plugin é revalidado (`provides_tools` muda), reabilitado e testado contra bancos locais (Postgres/Redis via Docker; SQL Server via container). Rollback = desabilitar o plugin.

## Open Questions

Nenhuma. A escolha de instâncias de banco para teste manual (container vs. instância existente) é operacional e não muda contratos nem breakdown.
