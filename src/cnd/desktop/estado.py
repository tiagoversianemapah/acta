"""Leitura do estado para a tela, e controle do processo do robô.

Separado da interface de propósito: aqui não há nenhum widget. Isso deixa
a parte que decide *o que mostrar* testável sem abrir janela nenhuma —
e testar interface gráfica é caro e frágil.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

from cnd.core import breaker
from cnd.infra import heartbeat
from cnd.infra.config import Config
from cnd.infra.db import conectar_leitura
from cnd.web import consultas


@dataclass
class Panorama:
    """Tudo que a tela precisa saber, numa leitura só do banco."""

    robo_ativo: bool
    robo_idade_s: float | None
    lote_id: int | None
    lote_nome: str
    resumos: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(r.total for r in self.resumos)

    @property
    def concluidos(self) -> int:
        return sum(r.concluidos for r in self.resumos)

    @property
    def falhados(self) -> int:
        return sum(r.falhados for r in self.resumos)

    @property
    def percentual(self) -> float:
        return (self.concluidos / self.total * 100) if self.total else 0.0

    @property
    def situacao(self) -> tuple[str, str]:
        """(texto, cor) do indicador de estado."""
        if self.robo_ativo:
            return "Robô em execução", "verde"
        if self.robo_idade_s is None:
            return "Robô parado", "cinza"
        return "Robô parado", "vermelho"

    def por_desfecho(self, desfecho: str) -> int:
        return sum(r.por_desfecho.get(desfecho, 0) for r in self.resumos)


def ler_panorama(cfg: Config) -> Panorama:
    """Uma foto do estado atual. Nunca levanta exceção: banco ausente ou
    ocupado devolve panorama vazio, e a tela mostra 'sem dados'."""
    try:
        conn = conectar_leitura(cfg.banco)
    except Exception:
        return Panorama(robo_ativo=False, robo_idade_s=None,
                        lote_id=None, lote_nome="—")

    try:
        idade = heartbeat.segundos_desde(conn, "orquestrador")
        lotes = consultas.lotes(conn)
        lote_id = lotes[0]["id"] if lotes else None
        nome = (lotes[0]["arquivo_origem"] or lotes[0]["descricao"]) if lotes else "—"
        resumos = [consultas.resumo(conn, orgao, lote_id)
                   for orgao in consultas.orgaos_do_lote(conn, lote_id)]
        return Panorama(
            robo_ativo=idade is not None and idade <= cfg.alertas.heartbeat_timeout_s,
            robo_idade_s=idade, lote_id=lote_id, lote_nome=nome, resumos=resumos,
        )
    except Exception:
        return Panorama(robo_ativo=False, robo_idade_s=None,
                        lote_id=None, lote_nome="—")
    finally:
        conn.close()


def listar_itens(cfg: Config, **filtros) -> list:
    try:
        conn = conectar_leitura(cfg.banco)
    except Exception:
        return []
    try:
        return consultas.jobs(conn, **filtros)
    except Exception:
        return []
    finally:
        conn.close()


def ler_atividade(cfg: Config, limite: int = 6) -> list[dict]:
    """As últimas consultas desta máquina. Nunca levanta exceção."""
    try:
        conn = conectar_leitura(cfg.banco)
    except Exception:
        return []
    try:
        return consultas.ultimas_tentativas(conn, limite)
    except Exception:
        return []
    finally:
        conn.close()


def ler_meses(cfg: Config) -> list[str]:
    """Meses com certidão guardada. Nunca levanta exceção."""
    from cnd.web.relatorio import meses_com_certidao

    try:
        conn = conectar_leitura(cfg.banco)
    except Exception:
        return []
    try:
        return meses_com_certidao(conn)
    except Exception:
        return []
    finally:
        conn.close()


def _comando_base() -> list[str]:
    """Como chamar a linha de comando a partir daqui.

    Rodando do código-fonte é o Python do ambiente. Rodando empacotado,
    `sys.executable` é o próprio ACTA.exe: usamos o cnd.exe que vem ao lado
    dele, que é a versão de console — o robô imprime o andamento, e a janela
    lê essa saída para mostrar no Registro. Um executável sem console
    entregaria essa saída no vazio.
    """
    if getattr(sys, "frozen", False):
        console = Path(sys.executable).with_name("cnd.exe")
        return [str(console if console.exists() else sys.executable)]
    return [sys.executable, "-m", "cnd.cli"]


class Robo:
    """O processo do orquestrador, controlado pela janela.

    Processo separado, e não thread, por dois motivos: a interface continua
    respondendo mesmo com o robô ocupado, e dá para encerrar de verdade —
    thread em Python não se mata no meio de uma operação.
    """

    def __init__(self, raiz: Path) -> None:
        self.raiz = raiz
        self._processo: subprocess.Popen | None = None
        self.linhas: Queue[str] = Queue()

    @property
    def rodando(self) -> bool:
        return self._processo is not None and self._processo.poll() is None

    def iniciar(self, limite: int | None = None,
                reiniciar_ritmo: bool = False) -> None:
        if self.rodando:
            return

        comando = [*_comando_base(), "rodar", "--forcar"]
        if limite:
            comando += ["--limite", str(limite)]
        if reiniciar_ritmo:
            comando.append("--reiniciar-ritmo")

        # A janela decodifica a saída do robô como UTF-8. Fora de um
        # console, o Python do Windows escreveria na codificação regional
        # (cp1252) e todo acento chegaria trocado no Registro — "órgão"
        # viraria "Ã³rgÃ£o". Mandar a codificação explícita tira a
        # adivinhação do caminho.
        ambiente = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

        self._processo = subprocess.Popen(
            comando, cwd=str(self.raiz), env=ambiente,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._ler_saida, daemon=True).start()

    def _ler_saida(self) -> None:
        processo = self._processo
        if processo is None or processo.stdout is None:
            return
        for linha in processo.stdout:
            self.linhas.put(linha.rstrip())
        self.linhas.put("— robô encerrado —")

    def parar(self) -> None:
        if not self.rodando:
            return
        self._processo.terminate()
        try:
            self._processo.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._processo.kill()

    def drenar(self, maximo: int = 200) -> list[str]:
        """Linhas novas do registro, sem travar a interface."""
        saida: list[str] = []
        for _ in range(maximo):
            try:
                saida.append(self.linhas.get_nowait())
            except Empty:
                break
        return saida


def estado_dos_orgaos(cfg: Config) -> dict[str, str]:
    """Disjuntor de cada órgão, para a tela poder oferecer 'retomar'."""
    try:
        conn = conectar_leitura(cfg.banco)
    except Exception:
        return {}
    try:
        return {o.codigo: breaker.consultar(conn, o.codigo).estado
                for o in cfg.ativos()}
    except Exception:
        return {}
    finally:
        conn.close()
