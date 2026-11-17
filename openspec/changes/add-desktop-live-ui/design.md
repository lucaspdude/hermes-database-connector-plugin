# Design: add-desktop-live-ui

## Context

As changes anteriores entregam estado e eventos: REST canônico em `/api/plugins/database-connector/` (conexões, queries, status com `last_seq`, health) e `query.*`/`connection.changed` como sinais. Fatos do SDK que moldam a UI (pesquisa do plano): a linha da sidebar não renderiza conteúdo (só `{ codicon, label, path, tier }`), `ctx.socket` é no-op em remoto OAuth e `broadcast_plugin_event` é fire-and-forget — evento invalida, REST reconcilia. O spike da fase 0.9 verifica essas superfícies no Desktop real — o Hermes do host `hostgator` (via ssh; headless — Desktop observado no app conectado ao mesmo perfil), com instâncias reais de SQL Server e Redis para os aceites — antes de construir.

## Goals / Non-Goals

**Goals:**

- Configurar conexões pela tela do app e ver a atividade de consultas em tempo real (página, sidebar, chip e indicador na lateral no desfecho A/B).
- Fechar o plugin: validação limpa, README completo, handover ao usuário.

**Non-Goals:**

- Mudanças de backend/tools — se o spike revelar a necessidade, volta como delta das capabilities existentes, não como código paralelo aqui.
- Escrita em banco, histórico maior, export, alertas (fase 2 do plano).
- Build step, JSX, cores hardcoded, `window.setInterval`.

## Decisions

1. **Spike antes da UI (fase 0.9)** — o plugin instalado no Hermes do host `hostgator` (via ssh, headless) e observado no app Hermes Desktop conectado ao mesmo perfil, na mesma versão do código analisado, responde seis perguntas (settings dinâmica, path efetivo da rota, badge na sidebar, id da row de sessão vs. `session_id` da tool, `ctx.rest` no gate, registro em `spike.md`). O desfecho A (badge por sessão) só é construído com o mapeamento de id confirmado; senão desfecho B (chip + card "último acesso por conexão"), com o desvio registrado — o requisito "indicador na lateral" é fechado em qualquer desfecho (R9).
2. **Estado via `useQuery` + invalidação** — `queries` e `status` com `refetchInterval: 5000`; eventos `query.*` e `connection.changed` chamam `queryClient.invalidateQueries`. O evento é acelerador, nunca fonte de verdade; polling reconcilia evento perdido (R3).
3. **Chip com semântica explícita** — `● N consultas · M bancos` onde N é **consultas ativas** (linhas "running"), `title`/`aria-label` detalham por conexão, clique navega via `host.navigate('/databases')` (path efetivo confirmado no spike), erro → `StatusDot` vermelho.
4. **Settings page com fallback declarado** — se o spike mostrar que `registerSettingsPage` só aceita campos declarativos (sem CRUD dinâmico), a config vira rota própria (`/databases/settings`) com link na página — decisão registrada no spike, não reaberta depois.
5. **Estilo e ciclo de vida do SDK** — ESM sem build, `jsx()`/`jsxs()` de `react/jsx-runtime`, só `@hermes/plugin-sdk` e `react*`; cores só `var(--ui-*)` e `StatusDot` do kit; timers/listeners só `ctx.setInterval`/`ctx.onEvent` (disposer devolvido pelo SDK).
6. **README como contrato de instalação e segurança** — sem pré-requisito de driver de sistema (install traz as deps), usuário somente-leitura como pré-requisito de **uso**, gate `plugins.enabled` (R5), toggle Desktop opt-in (R6), env vars de secret, Observability ▸ Logs, e nota de PII com decisão do usuário sobre `log_statement` (R8).

## Risks / Trade-offs

- [Spike negar form dinâmico na settings page] → rota `/databases/settings` própria (desvio previsto e documentado).
- [Mapeamento de `session_id` para a row de sessão não for confiável] → desfecho B do indicador; o requisito continua fechado com chip + card.
- [Evento perdido deixa chip torto] → reconciliação por `GET /status` (`active` + `last_seq`) no polling e ao receber qualquer evento.
- [Meia desktop stale no app] → README com o passo exato do toggle; `doctor` avisa cópia stale (R6).

## Migration Plan

Ativar o toggle do Desktop (Capabilities → Plugins), reiniciar o gateway e rodar a bateria manual do plano. Rollback = desabilitar o plugin; nenhum dado migra — o store já existe.

## Open Questions

Nenhuma que exija o usuário agora: as incógnitas de superfície são resolvidas pelo spike dentro desta change (tarefa 1), com registro em `spike.md` antes de construir a UI.
