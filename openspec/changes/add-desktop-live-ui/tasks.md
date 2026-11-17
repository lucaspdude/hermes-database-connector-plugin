# Tasks: add-desktop-live-ui

Implementa as fases 0.9, 2, 4 e 6 do plano e fecha a implementação completa do plugin. Pré-requisitos: `add-plugin-backend` e `add-agent-db-tools` implementadas. Feature branch + PR (AGENTS.md).

## 1. Spike de superfícies do Desktop (fase 0.9)

- [ ] 1.1 Instalar o plugin no Hermes do host `hostgator` (via ssh; headless — a metade desktop é observada no app Hermes Desktop conectado ao mesmo perfil), com o Desktop na mesma versão do código analisado, e verificar se `ctx.registerSettingsPage` renderiza em Settings → Plugins e aceita formulário dinâmico (lista de conexões, campos por engine, botão "Testar"); registrar evidência (print/log)
- [ ] 1.2 Descobrir o path efetivo da rota contribuída (`/databases` ou prefixada), testando clique real e não só registro; registrar o path que `host.navigate()` e a sidebar devem usar
- [ ] 1.3 Verificar se a linha da sidebar aceita badge/contagem dinâmica; registrar o resultado
- [ ] 1.4 Comparar o id entregue pela row de sessão (`SESSION_ROW_AREAS.trailing`) com o `session_id`/`conversation_id` do contexto da tool numa chamada sintética; registrar se o mapeamento é confiável
- [ ] 1.5 Confirmar que `ctx.rest` responde `GET /health` com o plugin em `plugins.enabled` e o mount `/api/plugins/database-connector/`; registrar o comportamento quando o plugin está desabilitado
- [ ] 1.6 Registrar os resultados em `docs/plans/hermes-plugins/database-connector/spike.md` e **decidir o desfecho A ou B** do indicador na lateral; ajustar as tarefas 4.x conforme o que o SDK permite

## 2. Página de atividade e navegação (fase 2)

- [ ] 2.1 Implementar a rota `/databases` (`ROUTES_AREA`) com a lista de atividade (hora, conexão, engine, statement truncado, duração, status) e o estado por conexão (último uso, ativa), usando `LogView`/tabela do kit; verificar com consultas reais que a lista atualiza em segundos
- [ ] 2.2 Adicionar a linha "Bancos" na sidebar (`SIDEBAR_NAV_AREA`) apontando para o path efetivo confirmado no spike; verificar clique navegando para a página
- [ ] 2.3 Adicionar "Abrir Bancos" na paleta (`PALETTE_AREA`); verificar execução navegando para a página
- [ ] 2.4 Ligar o estado via `useQuery` (`queries` e `status`, `refetchInterval: 5000`) com `queryClient.invalidateQueries` nos eventos `query.started`/`query.finished`/`query.failed` e `connection.changed`; verificar que um evento atualiza a lista imediatamente e que o polling corrige um evento perdido

## 3. Página de configuração (fase 2)

- [ ] 3.1 Implementar `ctx.registerSettingsPage` "Conexões" com lista + CRUD (criar/editar/excluir com confirmação) falando com `POST`/`PUT`/`DELETE /connections`; verificar criar/editar uma conexão pela tela e persistir entre restarts (ou aplicar o fallback `/databases/settings` decidido no spike)
- [ ] 3.2 Implementar campos condicionais por engine (SQL Server: driver/instance/database/encrypt/trust_server_certificate; Postgres: database/schema/sslmode; Redis: db_index/tls) com validação dos obrigatórios; verificar trocando a engine no form
- [ ] 3.3 Implementar `environment` como enum prod/homolog/local com badge visual distinto, sem default implícito, e confirmação explícita ao salvar em `prod`; verificar os dois comportamentos
- [ ] 3.4 Exibir o estado do segredo (`resolved`/`missing`/`unscoped`) com caminho para Settings ▸ Environment e detecção de duplicidade (engine+environment+host+database) com aviso antes de salvar; nunca exibir valor; verificar com conexão sem env var e com duplicata
- [ ] 3.5 Implementar a visão "Drivers" a partir de `GET /health` (disponibilidade por engine, comando `install` com botão de copiar, aviso "reinicie o gateway"); verificar num host sem um driver

## 4. Indicador ao vivo (fase 4)

- [ ] 4.1 Implementar o chip na status bar (`STATUSBAR_AREAS.right`) com `● N consultas · M bancos` (N = consultas ativas), `title`/`aria-label` detalhados, clique → página de atividade e erro → `StatusDot` vermelho; verificar com consulta lenta (`pg_sleep(5)`) que mostra a ativa e volta a zero
- [ ] 4.2 Implementar a reconciliação: eventos invalidam, polling de 5 s como fallback e `GET /status` (`active` + `last_seq`) como canônico; verificar simulando perda de evento (sem listener) e conferindo que o polling corrige o chip
- [ ] 4.3 Implementar o indicador na lateral conforme o desfecho do spike: **A** — badge por sessão (`SESSION_ROW_AREAS.trailing`) com conexão em uso + último acesso com timestamp; **B** — card "último acesso por conexão" na página com o desvio registrado no README/RISKS; verificar o aceite do plano: durante query lenta a lateral (ou o chip) mostra o banco e ao terminar mantém o último acesso com timestamp

## 5. Endurecimento e handover (fase 6)

- [ ] 5.1 Conferir estilo e ciclo de vida: só `var(--ui-*)` e componentes do kit (sem hex), timers/listeners só via `ctx.setInterval`/`ctx.onEvent`, ESM sem JSX; verificar alternância de tema claro/escuro e ausência de timers órfãos no teardown
- [ ] 5.2 Rodar `hermes plugins validate <dir> --install-deps` e `hermes plugins doctor <dir> --ci` com o plugin completo, no host `hostgator` (via ssh); confirmar saída limpa
- [ ] 5.3 Bateria manual completa do plano no Hermes do host `hostgator`, contra as instâncias reais de SQL Server e Redis (externas ao host; Postgres local/Docker no mac): criar conexão → testar → tool consulta → atividade em tempo real → chip mostra ativa → falha de banco → reconectar; registrar evidências no PR
- [ ] 5.4 Escrever o README do plugin: instalação sem pré-requisito de driver de sistema (install traz as deps), usuário de banco **somente-leitura** como pré-requisito de uso, gate `plugins.enabled`, toggle do Desktop, lista de env vars de secret, Observability ▸ Logs e (se desfecho B) o registro do desvio do indicador; verificar um usuário novo seguindo o README do zero ao chip funcionando
- [ ] 5.5 Registrar a nota de segurança sobre PII em `log_statement` e obter a decisão do usuário sobre manter o toggle ligado; verificar que a decisão está documentada
- [ ] 5.6 Decidir e documentar o destino do pacote (repo próprio para `hermes plugins install` / catálogo), conforme doc 02 da pesquisa
- [ ] 5.7 Abrir PR da feature branch para a `main` (Conventional Commits, CI verde, squash merge) com o resumo dos aceites da implementação completa
