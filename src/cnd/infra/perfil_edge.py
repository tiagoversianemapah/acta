"""O perfil do Edge no disco — cookies e marca de saída limpa.

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

**O Edge precisa estar morto de verdade.** O banco de cookies é um SQLite
dele, aberto com trava exclusiva: com o processo vivo, abrir o arquivo
falha com "unable to open database file" — e não com "database is locked",
que seria o erro esperado. Foi assim que a primeira versão desta limpeza
saiu com zero cookies removidos e o 400 continuou de pé. Fechar a janela
não basta; o processo de rede sobrevive alguns segundos a ela.

Como matar à força faz o Edge voltar com a bolha "Restaurar páginas" — que
rouba o foco e cobre a tela do robô cego —, `marcar_saida_limpa` desfaz
isso no arquivo de preferências do perfil.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from cnd.infra.log import obter

log = obter("infra.perfil_edge")


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


def limpar_cookies(dominio: str, raiz: Path | None = None) -> int:
    """Apaga os cookies do domínio (e subdomínios). Devolve quantos saíram.

    Zero com o perfil cheio é sinal de Edge ainda vivo — ver o cabeçalho
    deste módulo.
    """
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
                    extra={"arquivo": str(banco), "erro": str(erro),
                           "dica": "o Edge precisa estar encerrado"})
        return 0

    try:
        cursor = conexao.execute(
            "DELETE FROM cookies WHERE host_key = ? OR host_key LIKE ?",
            (dominio, f"%.{dominio}"),
        )
        conexao.commit()
        return max(cursor.rowcount, 0)
    except sqlite3.Error as erro:
        log.warning("cookies_nao_apagados",
                    extra={"arquivo": str(banco), "erro": str(erro),
                           "dica": "o Edge precisa estar encerrado"})
        return 0
    finally:
        conexao.close()


def preferencias(raiz: Path | None = None) -> list[Path]:
    raiz = raiz or raiz_do_edge()
    if not raiz.exists():
        return []
    return sorted(p for p in raiz.glob("*/Preferences") if p.is_file())


def marcar_saida_limpa(raiz: Path | None = None) -> int:
    """Diz ao Edge que o fechamento anterior foi normal.

    Sem isto, o Edge morto à força volta com "Restaurar páginas" — uma
    bolha que aparece por cima da página, rouba o foco e desalinha tudo que
    o robô cego mede por coordenada. São os dois campos que o próprio
    Chromium usa para decidir se mostra a bolha.
    """
    corrigidos = 0
    for arquivo in preferencias(raiz):
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError) as erro:
            log.warning("preferencias_do_edge_ilegiveis",
                        extra={"arquivo": str(arquivo), "erro": str(erro)})
            continue

        perfil = dados.get("profile")
        if not isinstance(perfil, dict):
            continue
        if perfil.get("exit_type") == "Normal" and perfil.get("exited_cleanly"):
            continue

        perfil["exit_type"] = "Normal"
        perfil["exited_cleanly"] = True
        try:
            arquivo.write_text(json.dumps(dados, ensure_ascii=False),
                               encoding="utf-8")
        except OSError as erro:
            log.warning("preferencias_do_edge_nao_gravadas",
                        extra={"arquivo": str(arquivo), "erro": str(erro)})
            continue
        corrigidos += 1
    return corrigidos
