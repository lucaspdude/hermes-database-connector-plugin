# AGENTS.md — regras para agentes e devs

## Regras básicas
- Não comitar direto na branch `main`.
- Conventional Commits no padrão: `tipo(escopo): descrição`.
- Sempre trabalhar com feature branches e abrir PRs para a `main`.

## Branches
- Nome: `<tipo>/<assunto-curto>` (ex.: `feat/queries-workitems`, `fix/timeout-auth`).
- Um PR = um objetivo; diffs pequenos e revisáveis.
- Atualizar com a `main` antes de abrir o PR.

## Conventional Commits
- Tipos: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
- Breaking change: `!` após o tipo (`feat!:`) ou rodapé `BREAKING CHANGE: <motivo>`.
- Descrição imperativa, minúscula, sem ponto final; o corpo opcional explica o porquê.
- A release é derivada desses commits (fix → patch, feat → minor, breaking → major); nada de mensagens vagas tipo `update`.

## PRs
- Checks/CI precisam passar antes do merge.
- Squash merge (1 commit limpo na `main`).
- Nunca `--force` em branches compartilhadas.

## Releases (automático)
- Merge na `main` dispara o workflow de release: cria a tag `vX.Y.Z` e a GitHub Release sozinho.
- Não criar tag/release manual.
