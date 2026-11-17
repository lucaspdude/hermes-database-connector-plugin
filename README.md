# hermes-database-connector-plugin

Agente lendo SQL Server, PostgreSQL e Redis com segurança: três tools
(`db_list_connections`, `db_query`, `db_redis_command`), log de atividade e
eventos de invalidação, além da meia desktop (página **Bancos**, chip de
status bar e tela de conexões em Settings ▸ Plugins).

**Destino do pacote:** este repo **é** o pacote instalável — o `hermes plugins
install <url-do-repo>` clona esta árvore e instala as três metades de uma vez
(agente `plugin.yaml`/`__init__.py`, backend `dashboard/plugin_api.py`, UI
`desktop/plugin.js`). Um id só (`database-connector`), uma namespace de API
(`/api/plugins/database-connector/`).

## Instalação

```bash
hermes plugins install <url-deste-repo>
```

1. O install **pergunta consentimento** e instala as dependências Python no
   ambiente gerenciado do Hermes (`psycopg[binary]`, `redis`, `pymssql` —
   declaradas no `plugin.yaml`). **Nenhum driver de sistema é necessário**
   (todos os drivers são wheels pip; o SQL Server usa `pymssql` com FreeTDS
   embutido — sem ODBC, sem `brew`).
2. Sem rede/wheel para algum driver? `hermes plugins install ... --no-deps`
   instala o plugin **desabilitado**; instale o driver à mão (o comando exato
   aparece na tela **Conexões ▸ Drivers**, copiável) e depois
   `hermes pm repair` re-sincroniza as deps declaradas.
3. **Depois de instalar um driver, reinicie o gateway** — um processo em
   execução nunca troca o ambiente de dependências.
4. Habilitação: o install habilita o plugin (`plugins.enabled` no perfil).
   **O backend só é importado com o plugin habilitado** — com ele desligado,
   `ctx.rest` responde 404 e o chip do Desktop mostra "bancos indisponível".

### Meia desktop (opt-in, app-level)

O `desktop/plugin.js` é copiado para `~/.hermes/desktop-plugins/<id>/` pelo
próprio app. Ligue o toggle do plugin em **Capabilities ▸ Plugins** (toggle
próprio, por app — não segue o seletor de perfil). A cópia é atualizada em
**Rescan** (ou `hermes plugins update`); o `hermes plugins doctor` avisa
quando a cópia está stale. Se a página não aparecer, ⌘K ▸ *Reload desktop
plugins*.

## Pré-requisito de uso: usuário somente-leitura

**Crie um usuário de banco com papel somente-leitura para cada conexão.** A
garantia primária contra escrita é essa credencial — não o filtro do plugin
(veja a política abaixo). O plugin vem com `read_only` fixo no MVP e recusa
statement de escrita na tool, mas o filtro é a segunda camada.

## Configuração (Settings ▸ Plugins ▸ Conexões)

A tela do Desktop cria/edita/exclui conexões (persistidas no store SQLite do
plugin, sobrevivem a restart):

- **Campos comuns:** id (slug), nome, engine, **environment** (prod/homolog/
  local — sem default; salvar em **prod** pede confirmação explícita), host,
  porta, database, usuário, `secret_ref`, habilitada.
- **Por engine:** SQL Server → driver ODBC (opcional), instância, encrypt,
  trust_server_certificate; PostgreSQL → schema, sslmode; Redis → db_index,
  TLS.
- **Segredo só por referência:** a conexão guarda o **nome** da env var
  (`secret_ref`); o valor vive no Environment do perfil e é resolvido em
  runtime por perfil ativo. A tela mostra o estado — `resolved`,
  `missing` ou `unscoped` — e nunca o valor; em `missing`, há caminho para
  Settings ▸ Environment.
- **Duplicidade:** salvar engine + ambiente + host + database idênticos a uma
  conexão existente mostra aviso antes de gravar.
- **Testar:** executa `POST /connections/{id}/test` contra o banco real
  (cada teste vira linha no log, `source: "ui"`).
- **Drivers:** sub-página com a disponibilidade por engine (de `GET /health`)
  e o comando `install` pronto para copiar quando falta algum.

### Variáveis de ambiente (segredos)

Uma env var por conexão, criada no Environment do perfil Hermes — exemplos:
`DB_PROD_SQLSERVER_PASSWORD`, `DB_HOMOLOG_POSTGRES_PASSWORD`,
`DB_LOCAL_REDIS_PASSWORD`. O nome é livre; é o que você preenche em
`secret_ref`.

## Desktop: atividade ao vivo

- **Página Bancos** (sidebar "Bancos" ou paleta ⌘K "Abrir Bancos"): lista de
  consultas recentes (hora, conexão, ambiente, engine, statement truncado e
  redigido, duração, status), card **último acesso por conexão** e estado
  ativo/ocioso.
- **Chip na status bar** (`● N consultas · M bancos`): N = consultas ativas;
  clique abre a página; backend fora → ponto vermelho "bancos indisponível".
- **Atualização:** eventos `query.started/finished/failed` e
  `connection.changed` invalidam; polling de 5 s e `GET /status`
  (`active` + `last_seq`) reconciliam evento perdido.

> **Desvio do indicador na lateral (desfecho B):** a linha da sidebar do
> Desktop não renderiza conteúdo dinâmico (só `{ path, label, codicon }`), e
> o badge por sessão (desfecho A) depende do spike 0.9 que confirma o
> mapeamento de ids sessão↔chat. Enquanto o spike não roda, o indicador na
> lateral é atendido pelo **chip de status bar + card "último acesso por
> conexão" na página** — o aceite do plano ("durante uma query lenta, a
> lateral ou o chip mostra o banco; ao terminar, mantém o último acesso com
> timestamp") fica coberto pelo chip e pelo card.

## API e eventos

REST em `/api/plugins/database-connector/` (montada pelo gateway com escopo
de secrets do perfil): `GET/POST/PUT/DELETE /connections`,
`POST /connections/{id}/test`, `GET /queries`, `GET /status`, `GET /health`,
`GET /ping`. Erros sempre `{ error, detail }`, sem stack trace, DSN ou
segredo. Eventos broadcast: `plugin.database-connector.query.started |
query.finished | query.failed | connection.changed` (sinal de invalidação —
o estado canônico é o REST).

## Observability ▸ Logs

O plugin loga no gateway padrão (visível em **Observability ▸ Logs**):
carregamento, consultas (metadados) e erros de driver limpos (sem senha/DSN).

## Nota de PII (`log_statement`) — decisão pendente

O `query_log` guarda o **statement** da consulta truncado (500 chars) e
redigido (literais mascarados, sem comentário). Mesmo redigido, o texto pode
conter nomes de tabela/coluna que identificam o domínio do negócio. O toggle
`log_statement` (config do plugin, padrão **ligado**) desliga o texto e
mantém só metadados. **Decisão do usuário sobre manter o padrão ligado:
pendente** (task 5.5 da change `add-desktop-live-ui`).

## Política read-only

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
