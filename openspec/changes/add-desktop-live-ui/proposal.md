# Proposal: add-desktop-live-ui

## Why

O requisito original do usuário é ver **em tempo real** quais consultas estão rodando e quais bancos estão sendo acessados, e configurar conexões pela tela do app. Backend (`add-plugin-backend`) e tools (`add-agent-db-tools`) já produzem esse estado; falta a metade desktop que o exibe. Esta é a **change 3 de 3** do plano (`docs/plans/hermes-plugins/database-connector/`), cobrindo as fases 0.9 (spike), 2, 4 e 6 — fecha a implementação completa do plugin.

## What Changes

- **Spike de superfícies do Desktop (fase 0.9)**: plugin mínimo contra o Desktop real para verificar o que o SDK permite — settings page dinâmica, path efetivo da rota, badge na linha da sidebar, id da row de sessão vs. `session_id` da tool, `ctx.rest` no gate `plugins.enabled`. Resultado registrado em `docs/plans/hermes-plugins/database-connector/spike.md` e decide o desfecho **A ou B** do indicador na lateral.
- **Página `/databases`** (`ROUTES_AREA`): atividade ao vivo (tabela hora/conexão/engine/statement truncado/duração/status) + estado por conexão (último uso, ativa), com `useQuery` (polling 5 s) e `queryClient.invalidateQueries` nos eventos `query.*` e `connection.changed`.
- **Linha "Bancos" na sidebar** (`SIDEBAR_NAV_AREA`) e **"Abrir Bancos" na paleta** (⌘K), navegando para `/databases`.
- **Página de configuração de conexões** (`ctx.registerSettingsPage`): CRUD com campos condicionais por engine, `environment` prod/homolog/local com badge distinto e confirmação explícita ao salvar em `prod`, estado do segredo (`resolved`/`missing`/`unscoped`) com link para Settings ▸ Environment, detecção de duplicidade (engine+environment+host+database) e tela "Drivers" com `{ available, module, install }` de `GET /health` + aviso de reinício do gateway.
- **Chip de status bar** (`STATUSBAR_AREAS.right`): `● N consultas · M bancos` com semântica definida (N = consultas ativas), `title`/`aria-label` detalhados, clique → `/databases`, erro → `StatusDot` vermelho; alimentado por `GET /status` com reconciliação por `last_seq`.
- **Indicador na lateral (requisito explícito)**: desfecho A — badge por sessão (`SESSION_ROW_AREAS.trailing`, conexão em uso + último acesso) — se o spike confirmar o mapeamento de ids; senão desfecho B — chip + card "último acesso por conexão" na página, com o desvio registrado (R9). Em qualquer um: durante query lenta a lateral (ou o chip) mostra o banco acessado; ao terminar mantém "último acesso" com timestamp.
- **Endurecimento e handover (fase 6)**: `validate`/`doctor --ci` limpos, bateria manual completa, README do plugin (instalação sem pré-requisito de driver de sistema, usuário de banco somente-leitura como pré-requisito de uso, gate `plugins.enabled`, toggle do Desktop, env vars de secret, Observability ▸ Logs), nota de PII sobre `log_statement` e decisão do destino do pacote.

## Capabilities

### New Capabilities

- `desktop-config-ui`: página de configuração de conexões (CRUD com validação, campos por engine, estados de segredo, duplicidade, tela de drivers) em Settings → Plugins.
- `live-activity-ui`: página `/databases`, entrada na sidebar, chip de status bar e indicador por sessão (desfecho A/B), com reconciliação evento + polling.

### Modified Capabilities

(nenhuma — consome `backend-api`, `database-tools`, `query-policy` e `activity-log` sem alterar seus requisitos; ajustes de spike que mudem contratos voltam como delta dessas capabilities)

## Impact

- **Meia desktop** (`desktop/plugin.js`) passa de esqueleto à implementação completa — ESM sem build, sem JSX (`jsx()`/`jsxs()` de `react/jsx-runtime`), só cores `var(--ui-*)` e teardown via `ctx.setInterval`/`ctx.onEvent`.
- **Contrato com o backend**: o evento invalida, o REST é canônico; polling de 5 s como fallback (`ctx.socket` só acelera — no-op em remoto OAuth).
- **Depende do spike**: o desfecho do indicador na lateral é decidido com evidência antes de construir a UI (fase 0.9); o resultado é registrado e o plano das superfícies ajustado.
- **Aceite final do plano**: durante `pg_sleep(5)` o chip mostra a consulta ativa e volta a zero; usuário novo segue o README do zero ao chip funcionando com produção e homologação configuradas.
