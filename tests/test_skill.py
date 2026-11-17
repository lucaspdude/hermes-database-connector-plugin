"""Task 4.1 — ``skills/database-connector/SKILL.md``.

O formato é o das skills embarcadas de plugin (front-matter ``name``/
``description`` + corpo em Markdown; ver
``docs/research/hermes-plugin/01-criando-plugins.md``, seção 9). Aqui só se
verifica que o arquivo existe, que o front-matter é parseável e que o corpo
cobre o que a task pede: quando usar cada tool, a política read-only com a
garantia primária, o significado dos ``code`` e os limites.

Se a skill aparece (e orienta corretamente) dentro do agente é verificação de
host real — **pendente** (Hermes no host ``hostgator``).
"""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "database-connector" / "SKILL.md"


def _load():
    assert SKILL.exists(), "SKILL.md tem que viver em skills/<id>/SKILL.md"
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _empty, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def test_front_matter_tem_name_e_description():
    front, _body = _load()

    assert front["name"] == "database-connector"  # = nome do diretório/registro
    assert "db_query" in front["description"]
    assert "read-only" in front["description"] or "read_only" in front["description"]


def test_corpo_cobre_as_tres_tools_e_a_ordem_de_uso():
    _front, body = _load()

    for tool in ("db_list_connections", "db_query", "db_redis_command"):
        assert tool in body
    # db_list_connections vem antes das outras (listar -> consultar)
    assert "Sempre primeiro." in body


def test_corpo_documenta_garantia_primaria_e_o_que_o_filtro_nao_pega():
    _front, body = _load()

    assert "somente-leitura" in body  # garantia primária: credencial do banco
    assert "WITH ... DELETE" in body
    assert "SELECT ... INTO" in body
    assert "EXEC" in body
    assert "SET TRANSACTION READ ONLY" in body  # por isso a transação existe


def test_corpo_explica_os_codes_e_os_limites():
    _front, body = _load()

    for code in ("read_only", "timeout", "not_found", "driver_missing", "secret_missing", "db_error"):
        assert f"`{code}`" in body
    assert "reinicie o gateway" in body  # nota do driver_missing
    assert "1000" in body  # teto de max_rows
    assert "64 KiB" in body  # truncamento Redis
    assert "10 s" in body and "30 s" in body  # timeouts padrão


def test_corpo_traz_exemplo_de_fluxo_listar_consultar_interpretar():
    _front, body = _load()

    assert '"connection_id"' in body
    assert '"statement"' in body
    assert '"environment": "prod"' in body
    assert "Interpretar o erro" in body
