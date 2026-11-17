# Spec Delta: live-activity-ui

## Purpose

Atividade de banco em tempo real no Hermes Desktop: página `/databases` com a lista ao vivo, entradas de navegação (sidebar e paleta), chip de status bar com consultas ativas e indicador na lateral (badge por sessão ou fallback), reconciliados por evento + polling.

## ADDED Requirements

### Requirement: Página de atividade ao vivo
A rota `/databases` SHALL exibir a lista de consultas recentes (hora, conexão, engine, statement truncado, duração, status) e o estado por conexão (último uso, ativa agora), alimentada por `GET /queries` e `GET /status`.

#### Scenario: Consulta aparece em tempo real
- **WHEN** o agente executa uma consulta enquanto a página está aberta
- **THEN** a consulta aparece na lista em segundos (evento ou polling), com conexão, engine e status

#### Scenario: Statement no log de atividade
- **WHEN** a lista exibe uma consulta com statement registrado
- **THEN** o statement aparece truncado e redigido, sem literais, senha ou DSN

### Requirement: Entradas de navegação
A sidebar SHALL incluir a linha "Bancos" e a paleta (⌘K) SHALL incluir "Abrir Bancos", ambas navegando para a página de atividade (no path efetivo confirmado pelo spike).

#### Scenario: Navegar pela sidebar
- **WHEN** o usuário clica em "Bancos" na sidebar
- **THEN** a página `/databases` abre

#### Scenario: Navegar pela paleta
- **WHEN** o usuário executa "Abrir Bancos" na paleta de comandos
- **THEN** a página `/databases` abre

### Requirement: Chip de status bar com consultas ativas
O chip na status bar SHALL mostrar `● N consultas · M bancos` onde N é o número de consultas ativas (linhas "running") e M o número de bancos em uso, com `title`/`aria-label` detalhando as conexões; clique SHALL navegar para a página de atividade; em erro de comunicação SHALL indicar falha (ponto vermelho).

#### Scenario: Consulta lenta visível
- **WHEN** uma consulta lenta (ex.: `pg_sleep(5)`) está em execução
- **THEN** o chip mostra a consulta como ativa em tempo real e volta a `0` quando ela termina

#### Scenario: Erro de backend
- **WHEN** `GET /status` falha (plugin desabilitado, gateway fora)
- **THEN** o chip indica erro visível em vez de número congelado

### Requirement: Reconciliação evento + polling
A UI SHALL tratar eventos como invalidação (re-buscar REST), SHALL manter polling de 5 s como fallback e SHALL reconciliar com `GET /status` (`active` + `last_seq`) — um evento perdido SHALL NEVER deixar o chip ou a lista permanentemente incorretos.

#### Scenario: Evento perdido reconciliado
- **WHEN** um `query.finished` se perde e o polling roda
- **THEN** a próxima leitura de `GET /status` corrige o chip e a lista sem intervenção

### Requirement: Indicador na lateral (desfecho A/B)
O indicador do banco acessado SHALL aparecer na lateral em um dos desfechos decididos pelo spike: **A** — badge na linha de sessão (`SESSION_ROW_AREAS.trailing`) mostrando a conexão em uso e, parado, o último acesso com timestamp; **B** — o chip de status bar mais o card "último acesso por conexão" na página, com o desvio registrado. Em qualquer desfecho: durante uma query lenta a lateral (ou o chip) mostra o banco acessado; ao terminar, mantém "último acesso" com timestamp.

#### Scenario: Desfecho A — badge por sessão
- **WHEN** o spike confirma o mapeamento de ids e uma consulta roda num chat
- **THEN** a linha daquele chat mostra o badge com a conexão em uso e, após terminar, o último acesso com timestamp

#### Scenario: Desfecho B — fallback registrado
- **WHEN** o spike não confirma o mapeamento de ids
- **THEN** o indicador é atendido pelo chip + card de último acesso e o desvio fica registrado no README/RISKS

### Requirement: Tema e ciclo de vida do SDK
A UI SHALL usar apenas tokens do tema (`var(--ui-*)`) e componentes do kit (ex.: `StatusDot`, `LogView`), SHALL registrar timers/listeners só via `ctx.setInterval`/`ctx.onEvent` e SHALL funcionar no tema claro e escuro.

#### Scenario: Alternância de tema
- **WHEN** o usuário alterna entre tema claro e escuro
- **THEN** a página e o chip mantêm contraste sem cores hardcoded

#### Scenario: Strings localizadas no padrão do app
- **WHEN** o usuário alterna o idioma do app (`display.language`)
- **THEN** todos os textos da UI vêm dos catálogos do plugin via `ctx.i18n.register` + `usePluginI18n` (en fallback + pt-BR), sem string hardcoded — superfícies estáticas (sidebar/paleta) se reconstruem em `onLocaleChange`
