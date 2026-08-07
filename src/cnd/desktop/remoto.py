"""Conversa com as máquinas da rede.

Cada máquina roda o seu robô e o seu painel; este módulo pergunta a todas
e junta as respostas. Nada de banco compartilhado: a máquina responde pelo
que ela sabe, e o aplicativo só monta o quadro geral.

Uma máquina fora do ar não derruba a tela — ela aparece como offline, que
é justamente a informação que importa.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from cnd.infra.config import Config, Maquina

TEMPO_LIMITE_S = 6


@dataclass
class EstadoRemoto:
    """O que uma máquina respondeu — ou por que não respondeu."""

    maquina: Maquina
    online: bool = False
    erro: str = ""
    dados: dict = field(default_factory=dict)

    @property
    def nome(self) -> str:
        return self.dados.get("maquina") or self.maquina.nome

    @property
    def rotulo(self) -> str:
        """O que vai em destaque no cartão: o órgão que a máquina atende.

        É o que a operação procura na tela — ninguém pensa "PC-CND-02",
        pensa "a máquina da Receita". O nome do computador continua logo
        abaixo, para quem for acessá-la.
        """
        return self.maquina.orgao or self.nome

    @property
    def subtitulo(self) -> str:
        partes = [self.nome] if self.maquina.orgao else []
        if self.maquina.url:
            partes.append(self.maquina.base.replace("http://", ""))
        return "  ·  ".join(partes)

    @property
    def robo_ativo(self) -> bool:
        return bool(self.dados.get("robo_ativo"))

    @property
    def orgaos(self) -> list[dict]:
        return self.dados.get("orgaos", [])

    @property
    def lote_id(self) -> int | None:
        return self.dados.get("lote_id")

    @property
    def lote_nome(self) -> str:
        return self.dados.get("lote_nome") or "—"

    @property
    def total(self) -> int:
        return sum(o["total"] for o in self.orgaos)

    @property
    def concluidos(self) -> int:
        return sum(o["concluidos"] for o in self.orgaos)

    @property
    def falhados(self) -> int:
        return sum(o["falhados"] for o in self.orgaos)

    @property
    def percentual(self) -> float:
        return (self.concluidos / self.total * 100) if self.total else 0.0

    def por_desfecho(self, desfecho: str) -> int:
        return sum(o["por_desfecho"].get(desfecho, 0) for o in self.orgaos)

    @property
    def suspensos(self) -> list[str]:
        return [o["orgao"] for o in self.orgaos if o.get("disjuntor") == "ABERTO"]

    @property
    def situacao(self) -> tuple[str, str]:
        if not self.online:
            return "Sem resposta", "cinza"
        if self.robo_ativo:
            return "Trabalhando", "verde"
        return "Robô parado", "vermelho"


def _pedir(maquina: Maquina, rota: str, senha: str, parametros: str = "") -> object:
    url = f"{maquina.base}{rota}{parametros}"
    pedido = urllib.request.Request(url)
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE_S) as resposta:
        return json.loads(resposta.read())


def consultar(maquina: Maquina, senha: str = "") -> EstadoRemoto:
    """Pergunta o panorama a uma máquina. Nunca levanta exceção."""
    try:
        return EstadoRemoto(maquina, online=True,
                            dados=_pedir(maquina, "/api/estado", senha))
    except urllib.error.HTTPError as erro:
        motivo = ("senha da rede recusada" if erro.code == 401
                  else f"a máquina respondeu {erro.code}")
        return EstadoRemoto(maquina, erro=motivo)
    except urllib.error.URLError as erro:
        return EstadoRemoto(maquina, erro=f"não respondeu ({erro.reason})")
    except Exception as erro:
        return EstadoRemoto(maquina, erro=f"{type(erro).__name__}: {erro}")


def consultar_local(cfg: Config) -> EstadoRemoto:
    """Lê o banco desta máquina direto, sem passar pela rede.

    Assim o aplicativo funciona numa instalação de máquina única sem exigir
    que o painel web esteja no ar — e o resto da tela não precisa saber a
    diferença, porque o formato é o mesmo.
    """
    from cnd.desktop.estado import ler_panorama

    panorama = ler_panorama(cfg)
    maquina = Maquina(cfg.rede.nome or "Esta máquina", "")
    return EstadoRemoto(maquina, online=True, dados={
        "maquina": maquina.nome,
        "robo_ativo": panorama.robo_ativo,
        "lote_id": panorama.lote_id,
        "lote_nome": panorama.lote_nome,
        "orgaos": [{
            "orgao": r.orgao, "total": r.total, "concluidos": r.concluidos,
            "pendentes": r.pendentes, "em_execucao": r.em_execucao,
            "falhados": r.falhados, "por_desfecho": r.por_desfecho,
            "percentual": round(r.percentual, 1), "intervalo_s": r.intervalo_s,
            "disjuntor": r.breaker_estado, "disjuntor_motivo": r.breaker_motivo,
            "ultima_tentativa": r.ultima_tentativa,
        } for r in panorama.resumos],
    })


def consultar_todas(cfg: Config) -> list[EstadoRemoto]:
    """Pergunta a todas ao mesmo tempo.

    Em paralelo de propósito: uma máquina desligada leva o tempo limite
    inteiro para responder, e em série isso somaria — cinco máquinas fora
    do ar travariam a tela por meio minuto.
    """
    if not cfg.rede.maquinas:
        return [consultar_local(cfg)]

    with ThreadPoolExecutor(max_workers=len(cfg.rede.maquinas)) as pool:
        return list(pool.map(lambda m: consultar(m, cfg.rede.senha),
                             cfg.rede.maquinas))


def listar_itens(maquina: Maquina, senha: str = "", **filtros) -> list[dict]:
    partes = [f"{chave}={urllib.parse.quote(str(valor))}"
              for chave, valor in filtros.items() if valor not in (None, "")]
    consulta = ("?" + "&".join(partes)) if partes else ""
    try:
        resultado = _pedir(maquina, "/api/itens", senha, consulta)
        return resultado if isinstance(resultado, list) else []
    except Exception:
        return []


def baixar(maquina: Maquina, rota: str, destino: Path, senha: str = "") -> Path:
    """Traz um arquivo da máquina (planilha ou pacote de certidões).

    O arquivo é gerado por ELA, na hora do pedido — o aplicativo não
    precisa de acesso ao disco da outra máquina nem a pasta compartilhada.
    """
    pedido = urllib.request.Request(f"{maquina.base}{rota}")
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    with urllib.request.urlopen(pedido, timeout=300) as resposta:
        destino.parent.mkdir(parents=True, exist_ok=True)
        with open(destino, "wb") as arquivo:
            while bloco := resposta.read(262144):
                arquivo.write(bloco)
    return destino
