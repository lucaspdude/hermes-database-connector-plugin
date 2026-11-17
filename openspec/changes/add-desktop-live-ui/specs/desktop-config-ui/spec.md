# Spec Delta: desktop-config-ui

## Purpose

Página de configuração de conexões do plugin em Settings → Plugins do Hermes Desktop: CRUD completo com campos condicionais por engine, ambientes com confirmação para produção, estado do segredo sem expor valor e tela de drivers para degradação orientada.

## ADDED Requirements

### Requirement: CRUD de conexões na tela de config
A página de configuração SHALL permitir criar, editar e excluir conexões (com confirmação de exclusão), refletindo o estado persistido do backend — o que a tela cria/edita SHALL aparecer na lista e persistir entre restarts.

#### Scenario: Criar pela tela
- **WHEN** o usuário preenche o formulário de nova conexão e salva
- **THEN** a conexão aparece na lista via `POST /connections` e continua lá após reiniciar o Hermes

#### Scenario: Excluir com confirmação
- **WHEN** o usuário solicita exclusão de uma conexão
- **THEN** recebe confirmação explícita antes do `DELETE /connections/{id}` e a conexão sai da lista

### Requirement: Campos condicionais por engine
O formulário SHALL exibir apenas os campos relevantes à engine selecionada — SQL Server: `driver` (caminho pyodbc), `instance?`, `database`, `encrypt`, `trust_server_certificate`; PostgreSQL: `database`, `schema?`, `sslmode`; Redis: `db_index`, `tls` — e SHALL validar campos obrigatórios por engine (`port` e `database` não são universais).

#### Scenario: Trocar engine muda o form
- **WHEN** o usuário troca a engine da conexão de `postgres` para `redis`
- **THEN** os campos de database/sslmode dão lugar a db_index/tls e a validação passa a exigir os campos de Redis

### Requirement: Ambiente explícito com confirmação de produção
`environment` SHALL ser enum prod/homolog/local com badge visual distinto, sem default implícito; salvar uma conexão em `prod` SHALL exigir confirmação explícita do usuário.

#### Scenario: Salvar em produção
- **WHEN** o usuário salva uma conexão com environment `prod`
- **THEN** recebe confirmação explícita antes de gravar

#### Scenario: Sem default de ambiente
- **WHEN** o formulário de nova conexão é aberto
- **THEN** nenhum environment vem pré-selecionado

### Requirement: Estado do segredo visível
Cada conexão SHALL exibir o estado do segredo (`resolved` / `missing` / `unscoped`) com um caminho para Settings ▸ Environment quando não resolvido — e SHALL NEVER exibir o valor do segredo.

#### Scenario: Segredo ausente
- **WHEN** a env var do `secret_ref` não existe no perfil ativo
- **THEN** a conexão aparece com estado "missing" e um link/caminho para Settings ▸ Environment, sem mostrar valor

#### Scenario: Segredo resolvido
- **WHEN** a env var existe no perfil ativo
- **THEN** o estado exibido é "resolved", sem revelar o valor

### Requirement: Detecção de duplicidade
Ao salvar, a tela SHALL detectar duplicidade provável (mesma engine + environment + host + database) e avisar o usuário antes de gravar.

#### Scenario: Duplicata avisada
- **WHEN** o usuário salva uma conexão idêntica em engine/ambiente/host/database a uma existente
- **THEN** recebe aviso de duplicidade antes da gravação e pode abortar

### Requirement: Tela de drivers
A página de configuração SHALL incluir a visão de drivers por engine a partir de `GET /health`: disponibilidade, nome do módulo e, para o que falta, o comando pronto com botão de copiar e o aviso de reiniciar o gateway.

#### Scenario: Driver faltando
- **WHEN** `GET /health` reporta uma engine com `available: false`
- **THEN** a tela mostra o comando `install` pronto para copiar e o aviso "reinicie o gateway" após instalar

#### Scenario: Host completo
- **WHEN** os três drivers estão disponíveis
- **THEN** a tela mostra as três engines disponíveis sem comandos pendentes
