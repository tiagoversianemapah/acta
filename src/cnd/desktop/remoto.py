"""Conversa com as máquinas da rede.

Cada máquina roda o seu robô e o seu painel; este módulo pergunta a todas
e junta as respostas. Nada de banco compartilhado: a máquina responde pelo
que ela sabe, e o aplicativo só monta o quadro geral.

Uma máquina fora do ar não derruba a tela — ela aparece como offline, que
é justamente a informação que importa.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from cnd.core import tempo
from cnd.infra.config import Config, Maquina, nome_do_orgao

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
    def anydesk(self) -> str:
        """O número para acessá-la.

        Vale primeiro o que a própria máquina informou, porque foi
        cadastrado por quem estava na frente dela. O da lista do console é
        a reserva — e é ele que salva quando ela está fora do ar, que é
        justamente quando alguém precisa entrar.
        """
        return self.dados.get("anydesk") or self.maquina.anydesk

    @property
    def acessavel(self) -> bool:
        """Se dá para entrar nela pelo AnyDesk."""
        return bool(self.anydesk)

    @property
    def local(self) -> bool:
        """Se é o próprio computador — o que não tem endereço de rede."""
        return not self.maquina.url

    @property
    def subtitulo(self) -> str:
        if self.local:
            return "este computador"
        partes = [self.nome] if self.maquina.orgao else []
        partes.append(self.maquina.base.replace("http://", ""))
        if not self.acessavel:
            partes.append("sem AnyDesk cadastrado")
        return "  ·  ".join(partes)

    @property
    def robo_ativo(self) -> bool:
        return bool(self.dados.get("robo_ativo"))

    @property
    def orgaos(self) -> list[dict]:
        return self.dados.get("orgaos", [])

    @property
    def atividade(self) -> list[dict]:
        """As últimas consultas daquela máquina, da mais recente para trás."""
        return self.dados.get("atividade", [])

    @property
    def lido_em(self) -> str:
        """Quando esta leitura chegou — só interessa se a máquina caiu."""
        return self.dados.get("lido_em", "")

    @property
    def tem_memoria(self) -> bool:
        """Se há dado antigo para mostrar de uma máquina que não responde."""
        return not self.online and bool(self.dados)

    @property
    def meses(self) -> list[str]:
        """Meses com certidão guardada, do mais recente para trás."""
        return self.dados.get("meses", [])

    @property
    def saude(self) -> dict:
        """Memória, disco e tempo ligada daquele computador."""
        return self.dados.get("saude", {})

    @property
    def roda_robo(self) -> bool:
        return self.dados.get("papel", "robo") == "robo"

    @property
    def rotulo_do_orgao(self) -> str:
        """O que a máquina faz, em nome de gente.

        Vem do órgão que ela atende — é a informação que identifica a
        máquina de fato. O `orgao` do config é só um apelido que o console
        dá a ela, e nem sempre está preenchido.
        """
        rotulos = [o.get("rotulo") or o["orgao"] for o in self.orgaos]
        return "  ·  ".join(rotulos) if rotulos else ""

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
    def pendentes(self) -> int:
        """Itens esperando consulta. Define se parar é normal ou incidente."""
        return sum(o.get("pendentes", 0) for o in self.orgaos)

    @property
    def suspensa(self) -> bool:
        return bool(self.suspensos)

    @property
    def situacao(self) -> tuple[str, str]:
        """Situação em uma palavra, e a cor dela.

        "Ociosa" em cinza, e não "parado" em vermelho, é a correção mais
        importante daqui: o robô roda por temporada, e ficar parado é o
        estado normal em uns 28 dias de cada 30. Vermelho o mês inteiro
        ensina a ignorar o vermelho — e aí ele não serve no dia em que a
        máquina realmente parar no meio do lote.
        """
        if not self.online:
            return "Sem resposta", "cinza"
        if self.pendentes and not self.robo_ativo:
            return "Parada com fila", "vermelho"
        if self.suspensa:
            return "Suspensa", "ambar"
        if self.robo_ativo:
            return "Trabalhando", "verde"
        # Verde, e não cinza: ociosa com a fila limpa é o estado saudável
        # do mês — a máquina fez o que tinha para fazer. Cinza sugeriria
        # que não se sabe o que está acontecendo com ela.
        return "Ociosa", "verde"

    @property
    def gravidade(self) -> int:
        """Para ordenar: quem tem problema aparece primeiro.

        Com quatro máquinas, a quebrada não pode ficar em terceiro por
        ordem alfabética — ela é o motivo de a tela ter sido aberta.
        """
        if not self.online:
            return 0
        if self.pendentes and not self.robo_ativo:
            return 1
        if self.suspensa:
            return 2
        if self.robo_ativo:
            return 3
        return 4


def _pedir(maquina: Maquina, rota: str, senha: str, parametros: str = "") -> object:
    url = f"{maquina.base}{rota}{parametros}"
    pedido = urllib.request.Request(url)
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE_S) as resposta:
        return json.loads(resposta.read())


def _postar(
    maquina: Maquina, rota: str, senha: str, dados: dict[str, str] | None = None
) -> object:
    url = f"{maquina.base}{rota}"
    corpo = urllib.parse.urlencode(dados or {}).encode("utf-8")
    pedido = urllib.request.Request(url, data=corpo, method="POST")
    pedido.add_header("Content-Type", "application/x-www-form-urlencoded")
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE_S) as resposta:
        return json.loads(resposta.read())


def consultar(maquina: Maquina, senha: str = "",
              lote: int | None = None) -> EstadoRemoto:
    """Pergunta o panorama a uma máquina. Nunca levanta exceção.

    Máquina muda devolve o ÚLTIMO ESTADO CONHECIDO, marcado como antigo. A
    versão anterior devolvia um cartão vazio — justo quando se mais quer
    saber dela. Saber que às 17h22 ela estava em 1.204 de 2.849 orienta
    quem vai decidir se espera ou vai lá; nada nenhum não orienta.

    `lote` pede os números de uma planilha específica. Uma leitura assim
    NÃO vira memória: ela é um recorte pedido na tela, e guardá-la faria a
    máquina fora do ar reaparecer depois mostrando só aquele pedaço.
    """
    parametros = f"?lote={lote}" if lote is not None else ""
    try:
        dados = _pedir(maquina, "/api/estado", senha, parametros)
        if lote is None:
            _guardar(maquina, dados)
        return EstadoRemoto(maquina, online=True, dados=dados)
    except urllib.error.HTTPError as erro:
        motivo = ("senha da rede recusada" if erro.code == 401
                  else f"a máquina respondeu {erro.code}")
    except urllib.error.URLError as erro:
        motivo = f"não respondeu ({erro.reason})"
    except Exception as erro:
        motivo = f"{type(erro).__name__}: {erro}"
    return EstadoRemoto(maquina, erro=motivo, dados=_lembrar(maquina))


def _arquivo_de_memoria(maquina: Maquina) -> Path:
    from cnd.infra.db import RAIZ_PROJETO

    seguro = "".join(c if c.isalnum() else "_" for c in maquina.base)
    return RAIZ_PROJETO / "data" / "ultimo_estado" / f"{seguro}.json"


def _guardar(maquina: Maquina, dados: dict) -> None:
    """Anota a resposta boa, com a hora em que chegou."""
    with contextlib.suppress(Exception):
        arquivo = _arquivo_de_memoria(maquina)
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(json.dumps({**dados, "lido_em": tempo.agora_iso()}),
                           encoding="utf-8")


def _lembrar(maquina: Maquina) -> dict:
    """O que ela disse por último, ou vazio se nunca respondeu."""
    with contextlib.suppress(Exception):
        return json.loads(_arquivo_de_memoria(maquina).read_text(
            encoding="utf-8"))
    return {}


def consultar_local(cfg: Config, lote: int | None = None) -> EstadoRemoto:
    """Lê o banco desta máquina direto, sem passar pela rede.

    Assim o aplicativo funciona numa instalação de máquina única sem exigir
    que o painel web esteja no ar — e o resto da tela não precisa saber a
    diferença, porque o formato é o mesmo.
    """
    import platform

    from cnd.core import breaker, controle
    from cnd.desktop.estado import ler_atividade, ler_meses, ler_panorama
    from cnd.infra import maquina
    from cnd.infra.db import conectar_leitura
    from cnd.infra.maquina import ler as ler_saude
    from cnd.web import consultas
    from cnd.web.consultas import eta_horas, rotulo_duracao

    adapter_cego = next(
        (o.adapter for o in cfg.ativos() if o.adapter in {"rfb_cego", "sefaz_es"}),
        "rfb_cego",
    )
    panorama = ler_panorama(cfg, lote)
    try:
        with contextlib.closing(conectar_leitura(cfg.banco)) as conn:
            lotes = consultas.lotes(conn)
            em_execucao = consultas.lote_em_execucao(conn)
            # Colhido AQUI, com a conexão viva: a lista de órgãos abaixo é
            # montada depois do `with`, e consultar de lá dava "Cannot
            # operate on a closed database".
            situacao_da_fila = {
                r.orgao: controle.situacao(conn, panorama.lote_id, r.orgao).situacao
                for r in panorama.resumos
            } if panorama.lote_id is not None else {}
    except Exception:
        lotes = []
        em_execucao = None
        situacao_da_fila = {}

    def _orgao_para_dict(r) -> dict:
        parametros = (
            cfg.orgaos[r.orgao].breaker if r.orgao in cfg.orgaos
            else breaker.ParametrosBreaker()
        )
        estado_breaker = breaker.EstadoBreaker(
            r.breaker_estado, r.breaker_ate, r.breaker_aberturas,
            r.breaker_motivo,
        )
        if not breaker.ativo_para(r.orgao, parametros):
            estado_breaker = breaker.EstadoBreaker(breaker.FECHADO, None, 0, None)
        pausa_s = breaker.cooldown_atual_s(estado_breaker, parametros)
        return {
            "orgao": r.orgao,
            # Estacionada, cancelada ou priorizada. Precisa vir por aqui
            # também, e não só pelo /api/estado: quando o console É a
            # máquina do robô, a tela lê deste caminho — e sem isto os
            # botões da fila apareciam todos como se nada estivesse
            # estacionado.
            "situacao_fila": situacao_da_fila.get(r.orgao, controle.ATIVA),
            "rotulo": (cfg.orgaos[r.orgao].rotulo if r.orgao in cfg.orgaos
                       else nome_do_orgao(r.orgao)),
            "total": r.total, "concluidos": r.concluidos,
            "pendentes": r.pendentes, "em_execucao": r.em_execucao,
            "falhados": r.falhados, "por_desfecho": r.por_desfecho,
            "percentual": round(r.percentual, 1), "intervalo_s": r.intervalo_s,
            "por_hora": r.ritmo_por_hora,
            "eta_horas": eta_horas(r),
            "disjuntor": estado_breaker.estado,
            "disjuntor_motivo": estado_breaker.motivo,
            "disjuntor_ate": estado_breaker.aberto_ate,
            "disjuntor_aberturas": estado_breaker.aberturas,
            "disjuntor_pausa_s": pausa_s,
            "disjuntor_pausa": rotulo_duracao(pausa_s),
            "ultima_tentativa": r.ultima_tentativa,
        }

    # Sem AnyDesk no cartão local: é o computador em que a pessoa já está,
    # e oferecer acesso remoto a si mesmo só confundiria.
    esta = Maquina(cfg.rede.nome or platform.node(), "")
    return EstadoRemoto(esta, online=True, dados={
        "maquina": esta.nome,
        "robo_ativo": panorama.robo_ativo,
        "lote_id": panorama.lote_id,
        "lote_nome": panorama.lote_nome,
        "lote_em_execucao": em_execucao,
        "atividade": ler_atividade(cfg, lote_id=panorama.lote_id),
        "meses": ler_meses(cfg),
        "lotes": [{"id": lote["id"], "descricao": lote["descricao"],
                   "arquivo": lote["arquivo_origem"],
                   "itens": lote["jobs"],
                   "criado_em": lote["criado_em"],
                   "encerrado_em": lote["encerrado_em"]}
                  for lote in lotes],
        "saude": ler_saude(cfg.pasta_certidoes).como_dicionario(),
        "papel": "robo" if cfg.rede.roda_robo else "console",
        "versao": maquina.versao(),
        "calibragem": maquina.calibragem(
            cfg.pasta_certidoes.parent / "calibragem", adapter_cego),
        "certidoes": maquina.certidoes(cfg.pasta_certidoes),
        "ultimo_sinal_ha_s": (round(panorama.robo_idade_s)
                              if panorama.robo_idade_s is not None else None),
        "orgaos": [_orgao_para_dict(r) for r in panorama.resumos],
    })


def consultar_todas(cfg: Config) -> list[EstadoRemoto]:
    """Pergunta a todas ao mesmo tempo.

    Em paralelo de propósito: uma máquina desligada leva o tempo limite
    inteiro para responder, e em série isso somaria — cinco máquinas fora
    do ar travariam a tela por meio minuto.
    """
    if not cfg.rede.maquinas:
        # Console sem máquinas cadastradas não se mostra como se fosse uma:
        # ele não emite certidão nenhuma, e listá-lo faria parecer que há um
        # robô onde não há. Volta lista vazia, e a tela explica o que falta.
        return [] if not cfg.rede.roda_robo else [consultar_local(cfg)]

    with ThreadPoolExecutor(max_workers=len(cfg.rede.maquinas)) as pool:
        return list(pool.map(lambda m: consultar(m, cfg.rede.senha),
                             cfg.rede.maquinas))


def listar_itens(maquina: Maquina, senha: str = "",
                 **filtros) -> list[dict] | None:
    """Itens daquela máquina, ou None se ela não respondeu.

    None e lista vazia são coisas diferentes: devolver `[]` para máquina
    fora do ar faria a tela dizer "nenhum item" quando a verdade é "não
    sei" — e a pessoa concluiria que a planilha não entrou.
    """
    partes = [f"{chave}={urllib.parse.quote(str(valor))}"
              for chave, valor in filtros.items() if valor not in (None, "")]
    consulta = ("?" + "&".join(partes)) if partes else ""
    try:
        resultado = _pedir(maquina, "/api/itens", senha, consulta)
        return resultado if isinstance(resultado, list) else None
    except Exception:
        return None


def contar_itens(maquina: Maquina, senha: str = "", **filtros) -> int | None:
    filtros = {**filtros, "limite": 1, "incluir_total": "true"}
    partes = [
        f"{chave}={urllib.parse.quote(str(valor))}"
        for chave, valor in filtros.items()
        if valor not in (None, "")
    ]
    consulta = ("?" + "&".join(partes)) if partes else ""
    try:
        resultado = _pedir(maquina, "/api/itens", senha, consulta)
        if isinstance(resultado, dict):
            return int(resultado.get("total") or 0)
        if isinstance(resultado, list):
            return len(resultado)
    except Exception:
        return None
    return None


def ja_na_fila_no_mes(
    maquina: Maquina, senha: str = "", orgao: str = "", mes: str = "",
) -> dict | None:
    partes = [
        f"{chave}={urllib.parse.quote(str(valor))}"
        for chave, valor in {"orgao": orgao, "mes": mes}.items()
        if valor
    ]
    consulta = ("?" + "&".join(partes)) if partes else ""
    try:
        resultado = _pedir(maquina, "/api/fila/mes", senha, consulta)
        return resultado if isinstance(resultado, dict) else None
    except Exception:
        return None


def orgaos_do_mes(
    maquina: Maquina, senha: str = "", mes: str = "",
) -> list[dict] | None:
    consulta = f"?mes={urllib.parse.quote(mes)}" if mes else ""
    try:
        resultado = _pedir(maquina, "/api/orgaos/mes", senha, consulta)
        return resultado if isinstance(resultado, list) else None
    except Exception:
        return None


def reenfileirar_falhados(
    maquina: Maquina, senha: str = "", orgao: str | None = None,
    lote_id: int | None = None,
) -> dict | None:
    try:
        resultado = _postar(
            maquina, "/api/reenfileirar", senha,
            {"orgao": orgao or "", "lote_id": str(lote_id or 0)},
        )
        return resultado if isinstance(resultado, dict) else None
    except Exception:
        return None


def retomar_todas_as_pausas(maquina: Maquina, senha: str = "") -> dict | None:
    """Destrava todos os órgãos da máquina — o botão sempre visível."""
    try:
        resultado = _postar(maquina, "/api/breaker/retomar", senha)
    except Exception:
        return None
    return resultado if isinstance(resultado, dict) else None


def retomar_pausa(maquina: Maquina, orgao: str, senha: str = "") -> dict | None:
    try:
        resultado = _postar(maquina, f"/api/breaker/{orgao}/retomar", senha)
        return resultado if isinstance(resultado, dict) else None
    except Exception:
        return None


def tentativas_do_job(maquina: Maquina, senha: str,
                      job_id: int) -> list[dict]:
    """O histórico daquele item, buscado na máquina que o processou."""
    try:
        resultado = _pedir(maquina, f"/api/tentativas/{job_id}", senha)
        return resultado if isinstance(resultado, list) else []
    except Exception:
        return []


def diagnostico(maquina: Maquina, senha: str = "", dias: int = 7) -> dict | None:
    """Diagnóstico operacional da máquina, ou None se ela não respondeu."""
    try:
        resultado = _pedir(
            maquina, "/api/diagnostico", senha,
            f"?dias={urllib.parse.quote(str(dias))}",
        )
        return resultado if isinstance(resultado, dict) else None
    except Exception:
        return None


@dataclass
class Entrega:
    """O resultado de juntar as certidões do mês de todas as máquinas."""

    arquivos: int = 0
    por_maquina: dict = field(default_factory=dict)
    falhas: dict = field(default_factory=dict)

    @property
    def resumo(self) -> str:
        partes = [f"{nome}: {n}" for nome, n in self.por_maquina.items()]
        return "\n".join(partes)


def baixar_certidoes(cfg: Config, mes: str, destino: Path,
                     somente_negativas: bool = False,
                     orgao: str | None = None) -> Entrega:
    """Junta num pacote só as certidões do mês de todas as máquinas.

    Cada máquina monta o pacote dela, já com uma pasta por órgão, e aqui as
    entradas são copiadas para um pacote único. É o que o cliente recebe:

        RECEITA FEDERAL/CERTIDOES NEGATIVAS/...
        SEFAZ GOIAS/CERTIDOES NEGATIVAS/...
        indice.csv

    Vai para arquivo temporário e não para a memória: um mês completo passa
    de 100 MB por máquina, e segurar três desses de uma vez derrubaria a
    janela em computador de escritório.

    Máquina que não responde não impede a entrega — ela entra em `falhas`,
    para quem baixou saber o que ficou de fora em vez de descobrir depois.
    """
    entrega = Entrega()
    consulta = f"?somente_negativas={'1' if somente_negativas else '0'}"
    if orgao:
        consulta += f"&orgao={urllib.parse.quote(orgao)}"
    destino.parent.mkdir(parents=True, exist_ok=True)

    indice_csv = StringIO()
    indice = csv.writer(indice_csv, delimiter=";", lineterminator="\n")
    indice.writerow(["orgao", "tipo", "empresa", "documento", "emitida_em",
                     "valida_ate", "arquivo"])

    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as pacote:
        for maquina in (cfg.rede.maquinas or (None,)):
            nome = maquina.nome if maquina else (cfg.rede.nome or "esta máquina")
            try:
                origem = _pacote_da_maquina(cfg, maquina, mes, consulta)
            except Exception as erro:
                entrega.falhas[nome] = f"{type(erro).__name__}: {erro}"
                continue

            try:
                with zipfile.ZipFile(origem) as vindo:
                    for item in vindo.infolist():
                        if item.filename == "indice.csv":
                            linhas = csv.reader(
                                StringIO(vindo.read(item).decode("utf-8")),
                                delimiter=";",
                            )
                            next(linhas, None)   # o cabeçalho é um só
                            indice.writerows(linhas)
                            continue
                        if item.filename in pacote.namelist():
                            continue        # mesmo órgão em duas máquinas
                        pacote.writestr(item, vindo.read(item))
                        entrega.arquivos += 1
                        entrega.por_maquina[nome] = entrega.por_maquina.get(nome, 0) + 1
            finally:
                if isinstance(origem, Path):
                    origem.unlink(missing_ok=True)

        pacote.writestr("indice.csv", indice_csv.getvalue())
    return entrega


def _pacote_da_maquina(cfg: Config, maquina: Maquina | None, mes: str,
                       consulta: str) -> Path:
    """Traz (ou monta) o pacote de uma máquina, num arquivo temporário."""
    descritor, nome_temporario = tempfile.mkstemp(suffix=".zip", prefix="acta_")
    os.close(descritor)
    temporario = Path(nome_temporario)

    if maquina is None:
        from cnd.infra.db import conectar_leitura
        from cnd.web.relatorio import zipar_pdfs

        pedido = urllib.parse.parse_qs(consulta.lstrip("?"))
        with contextlib.closing(conectar_leitura(cfg.banco)) as conn:
            temporario.write_bytes(zipar_pdfs(
                conn, mes, pedido.get("somente_negativas") == ["1"],
                nomes={c: o.rotulo for c, o in cfg.orgaos.items()},
                orgao=pedido.get("orgao")))
        return temporario

    baixar(maquina, f"/certidoes/{mes}.zip{consulta}", temporario,
           cfg.rede.senha)
    return temporario


def baixar_planilha(cfg: Config, mes: str, destino: Path,
                    orgao: str | None = None) -> Entrega:
    """A planilha do mês. De uma máquina só, ou desta se não houver rede.

    Diferente das certidões, a planilha não se junta: cada máquina produz
    um arquivo com abas próprias, e mesclar planilhas do Excel entregaria
    algo pior que o original. Com várias máquinas, exporta-se a do órgão
    escolhido — que é justamente para isso que o filtro existe.
    """
    entrega = Entrega()
    consulta = f"?orgao={urllib.parse.quote(orgao)}" if orgao else ""
    destino.parent.mkdir(parents=True, exist_ok=True)

    alvo = next((m for m in cfg.rede.maquinas
                 if not orgao or m.orgao), None) if cfg.rede.maquinas else None
    nome = alvo.nome if alvo else (cfg.rede.nome or "esta máquina")
    try:
        if alvo is None:
            from cnd.infra.db import conectar_leitura
            from cnd.web.relatorio import Recorte, gerar_bytes

            with contextlib.closing(conectar_leitura(cfg.banco)) as conn:
                destino.write_bytes(gerar_bytes(conn, Recorte(mes, orgao)))
        else:
            baixar(alvo, f"/relatorio/{mes}.xlsx{consulta}", destino,
                   cfg.rede.senha)
        entrega.arquivos = 1
        entrega.por_maquina[nome] = 1
    except Exception as erro:
        entrega.falhas[nome] = f"{type(erro).__name__}: {erro}"
    return entrega


def enviar_planilha(maquina: Maquina, arquivo: Path, senha: str = "",
                    aba: str = "", orgao: str = "", nome: str = "") -> dict:
    """Sobe a planilha para a máquina e devolve o resumo da importação.

    `aba` diz onde estão os dados; `orgao` diz qual automação vai rodar.
    Sem `aba` e sem `orgao`, a máquina importa todas as abas conhecidas pelo
    nome. Esse é o caminho do botão "Enviar planilha" do app de mesa.
    """
    limite = b"----acta" + str(id(arquivo)).encode()
    corpo = b"".join([
        b"--", limite, b"\r\n",
        b'Content-Disposition: form-data; name="aba"\r\n\r\n',
        aba.encode("utf-8"), b"\r\n--", limite, b"\r\n",
        b'Content-Disposition: form-data; name="orgao"\r\n\r\n',
        orgao.encode("utf-8"), b"\r\n--", limite, b"\r\n",
        b'Content-Disposition: form-data; name="nome"\r\n\r\n',
        (nome or arquivo.name).encode("utf-8"), b"\r\n--", limite, b"\r\n",
        b'Content-Disposition: form-data; name="arquivo"; filename="',
        arquivo.name.encode("utf-8"), b'"\r\n',
        b"Content-Type: application/vnd.openxmlformats-officedocument"
        b".spreadsheetml.sheet\r\n\r\n",
        arquivo.read_bytes(), b"\r\n--", limite, b"--\r\n",
    ])
    pedido = urllib.request.Request(
        f"{maquina.base}/api/planilha", data=corpo, method="POST")
    pedido.add_header("Content-Type",
                      f"multipart/form-data; boundary={limite.decode()}")
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    with urllib.request.urlopen(pedido, timeout=120) as resposta:
        return json.loads(resposta.read())


def comandar_robo(maquina: Maquina, iniciar: bool, senha: str = "") -> dict:
    """Liga ou para o robô daquela máquina."""
    rota = "/api/robo/iniciar" if iniciar else "/api/robo/parar"
    pedido = urllib.request.Request(f"{maquina.base}{rota}", data=b"",
                                    method="POST")
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resposta:
            return json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        # O corpo traz o motivo em português — área de trabalho bloqueada,
        # senha não configurada. Perdê-lo deixaria só "HTTP 409".
        with contextlib.suppress(Exception):
            raise RuntimeError(json.loads(erro.read())["detail"]) from erro
        raise


def atualizar(maquina: Maquina, senha: str, origem: str,
              sha256: str) -> dict:
    """Pede para a maquina baixar e aplicar o pacote publicado pelo console.

    `sha256` nao tem padrao de proposito: a maquina do outro lado recusa
    sem ele, e um argumento opcional aqui so adiaria a recusa para depois
    da ida a rede - sem dizer a quem chama que faltou o hash.
    """
    corpo = urllib.parse.urlencode({
        "origem": origem,
        "sha256": sha256,
    }).encode()
    pedido = urllib.request.Request(
        f"{maquina.base}/api/atualizar", data=corpo, method="POST"
    )
    pedido.add_header("Content-Type", "application/x-www-form-urlencoded")
    if senha:
        pedido.add_header("X-CND-Senha", senha)
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resposta:
            return json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        with contextlib.suppress(Exception):
            raise RuntimeError(json.loads(erro.read())["detail"]) from erro
        raise


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
