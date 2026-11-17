# Design: add-plugin-backend

## Context

Repo greenfield: o plugin `database-connector` ainda não existe. O plano (`docs/plans/hermes-plugins/database-connector/`) e a pesquisa base já verificaram no Hermes v0.23.1: `plugin_data_dir`/`plugin_db` (SQLite WAL), `agent.secret_scope.get_secret`, `broadcast_plugin_event` (fire-and-forget), o mount `/api/plugins/<id>/` e o gate `plugins.enabled` (o backend só é importado com o plugin habilitado). Esta change cria o pacote completo com backend funcional; as tools do agente e a UI são as changes seguintes.

## Goals / Non-Goals

**Goals:**

- Pacote instalável e habilitável (`validate`/`doctor` limpos), com as três metades esqueletadas e o backend plenamente funcional.
- Store SQLite única (tabelas `connections` e `query_log`) acessada por rotas e, depois, pelas tools — um só estado.
- Degradação por engine: driver ausente não derruba o plugin nem as outras engines.

**Non-Goals:**

- Tools do agente e execução de consultas (change `add-agent-db-tools`).
- Qualquer tela do Desktop além do esqueleto mínimo (change `add-desktop-live-ui`).
- Escrita em banco, pool de conexões, criptografia de segredo inline.

## Decisions

1. **`service.py` no root do pacote como fonte única de lógica** — `plugin_api.py` é adaptador HTTP fino (rotas → funções do service). Alternativa descartada: lógica dentro das rotas; duplicaria estado quando as tools importarem o mesmo módulo (duas cópias = dois estados).
2. **SQLite via `plugin_db`** — WAL + `foreign_keys=True`, `busy_timeout`, escrita sempre em transação curta. Alternativa descartada: JSON em `plugin_data_dir` (sem concorrência segura entre thread da tool e event loop da API — risco R7 do plano).
3. **Migração versionada do schema** — tabelas `connections` e `query_log` do design do plano criadas já nesta change; a tabela de log fica sem produtores até a change de tools, mas o contrato de leitura (`GET /queries`, `GET /status`) já fica estável para a UI.
4. **Segredo só por referência** — a conexão guarda o nome da env var; o valor é resolvido em runtime com `get_secret` por perfil ativo, com estados `resolved`/`missing`/`unscoped` (`UnscopedSecretError` em multiplexação sem escopo → erro claro, sem recorrer a `os.environ`). Alternativa descartada: segredo inline no SQLite (texto claro, R2).
5. **Dependências declaradas no manifesto** (`python_dependencies`) — o `hermes plugins install` pergunta consentimento e instala os três drivers via PM/uv; o plugin nunca roda `pip install` (o scanner `plugin_guard` bloqueia). Alternativa descartada: instalação em runtime.
6. **Evento como sinal de invalidação** — `connection.changed` não carrega estado; quem recebe re-busca REST. Consistente com `broadcast_plugin_event` ser fire-and-forget e `ctx.socket` ser no-op em remoto OAuth.
7. **Detecção de driver com `importlib.util.find_spec`** (sem importar) — `GET /health` devolve `{ available, module, install }` com o comando pronto; import lazy na execução futura. Evita que um `ImportError` derrube o carregamento do plugin (R1). O comando `install` resolve o caminho do `uv` antes de montar a sugestão: no host-alvo (`hostgator`, AlmaLinux 9) o binário não está no PATH — fica vendorizado em `~/.hermes/tools/uv-*/uv`; glob nesse caminho com fallback para `uv`.
8. **O repo é o próprio pacote** — `plugin.yaml` na raiz do repositório: o `hermes plugins install` lê a árvore carregável do clone (`subdir` existe só para monorepo). A documentação do plano fica em `docs/plans/hermes-plugins/database-connector/`, não dentro do pacote.
9. **Retenção do `query_log` com cap de 1.000 linhas** — prune das linhas mais antigas na mesma transação da escrita; evita crescimento sem limite sem migrar schema nem agendar tarefa.
10. **Teste de conexão logado** — cada `POST /connections/{id}/test` registra linha no `query_log` (`source: "ui"`, `statement` NULL); se virar ruído, basta parar de escrever a linha.
11. **Testes automatizados e CI** — pytest com fakes de `ctx` (a pesquisa não documenta harness de testes do Hermes) e workflow de CI no repo; validação e aceite no Hermes real do host `hostgator` (via ssh), com instâncias reais de SQL Server e Redis (externas ao host) e Postgres local/Docker no mac.

## Risks / Trade-offs

- [Host sem rede ou sem wheel → enable recusado] → escape documentado `--no-deps` + instalação manual orientada pela tela de drivers + `hermes pm repair`.
- [Driver instalado depois não entra no processo em execução] → `/health` e a UI avisam "reinicie o gateway".
- [Backend 404 silencioso quando o plugin não está em `plugins.enabled`] → mensagem de erro clara nas rotas/README do plugin (R5).
- [Concorrência SQLite entre thread e event loop] → WAL + `busy_timeout` + transações curtas (R7).
- [`query_log` sem produtores nesta fase] → aceito deliberadamente; evita migrar schema duas vezes e congela o contrato de leitura.

## Migration Plan

Greenfield: criar o pacote, rodar `hermes plugins validate --install-deps` e `hermes plugins doctor --ci`, habilitar no perfil e ligar o toggle do Desktop. Rollback = desabilitar o plugin; o store vive em `plugin-data/database-connector/` e sobrevive a reinstalação.

## Open Questions

Nenhuma que afete specs ou breakdown desta change. O spike de superfícies do Desktop (fase 0.9 do plano) pertence à change `add-desktop-live-ui` e só decide o desfecho A/B do indicador na lateral.
