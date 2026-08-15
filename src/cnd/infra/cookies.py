"""Cookies do Edge — remoção cirúrgica, por domínio.

Existe por causa de uma tela vista em 15/08/2026, no meio de um lote:

    400 Bad Request
    Request Header Or Cookie Too Large
    nginx/1.28.3

Quem recusa é o servidor do portal, antes de a aplicação rodar: a cada
emissão a Receita devolve mais cookies, eles se acumulam no perfil do Edge
e, passado o limite do nginx, TODA requisição àquele domínio volta 400.
Não adianta esperar nem reiniciar o navegador — o cabeçalho grande está
gravado no disco, e vai junto na próxima vez.

Apagar só os cookies daquele domínio resolve e não custa nada ao operador:
o que ele tem de login em outros sites continua intacto. É por isso que
esta função não usa a "limpeza de dados de navegação" do Edge, que levaria
tudo.

O banco de cookies é um SQLite do próprio Edge e fica travado enquanto ele
roda — sempre chame com o navegador fechado.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from cnd.infra.log import obter

log = obter("infra.cookies")


def raiz_do_edge() -> Path:
    """Pasta de perfis do Edge nesta máquina."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Microsoft" / "Edge" / "User Data"


def bancos_de_cookies(raiz: Path | None = None) -> list[Path]:
    """Um banco por perfil (Default, Profile 1, ...).

    O robô usa o perfil padrão, mas apagar em todos é mais seguro do que
    adivinhar qual o Edge abriu — e apagar cookie de um domínio só, em um
    perfil que não era o nosso, não quebra nada.
    """
    raiz = raiz or raiz_do_edge()
    if not raiz.exists():
        return []
    return sorted(p for p in raiz.glob("*/Network/Cookies") if p.is_file())


def limpar_dominio(dominio: str, raiz: Path | None = None) -> int:
    """Apaga os cookies do domínio (e subdomínios). Devolve quantos saíram."""
    total = 0
    for banco in bancos_de_cookies(raiz):
        total += _limpar_banco(banco, dominio)
    return total


def _limpar_banco(banco: Path, dominio: str) -> int:
    # host_key vem em duas formas: o domínio cru ("servicos.receitafederal
    # .gov.br") e a forma com ponto na frente (".receitafederal.gov.br"),
    # usada pelos cookies válidos para todos os subdomínios. Comparar com
    # "%dominio" pegaria também um "falsoreceitafederal.gov.br".
    try:
        conexao = sqlite3.connect(banco, timeout=5.0)
    except sqlite3.Error as erro:
        log.warning("banco_de_cookies_nao_abriu",
                    extra={"arquivo": str(banco), "erro": str(erro)})
        return 0

    try:
        cursor = conexao.execute(
            "DELETE FROM cookies WHERE host_key = ? OR host_key LIKE ?",
            (dominio, f"%.{dominio}"),
        )
        conexao.commit()
        return max(cursor.rowcount, 0)
    except sqlite3.Error as erro:
        # Quase sempre "database is locked": o Edge ainda está no ar.
        log.warning("cookies_nao_apagados",
                    extra={"arquivo": str(banco), "erro": str(erro)})
        return 0
    finally:
        conexao.close()
