"""O vigia: o que precisa virar e-mail enquanto o robô trabalha.

Roda no laço principal, fora dos workers — assim continua olhando mesmo
quando todos os órgãos estão parados, que é justamente quando alguém
precisa ser avisado.

A divisão com `vigilancia.py` é de propósito: lá ficam as PERGUNTAS puras
sobre o banco ("o lote acabou?", "parou de andar?"), testáveis sem thread
nem e-mail; aqui fica quem as faz de tempos em tempos, decide o texto e
manda o aviso.
"""
from __future__ import annotations

import time

from cnd.core import breaker, fila, recuperacao, tempo
from cnd.core.modelos import Status
from cnd.infra import alertas
from cnd.infra.config import Config, ConfigOrgao
from cnd.infra.log import obter
from cnd.orquestrador import vigilancia

log = obter("orquestrador")

# Um sinal de vida mais velho que isto é orquestrador morto, não ocupado:
# o heartbeat bate de minuto em minuto (ver worker.INTERVALO_HEARTBEAT_S).
SEGUNDOS_PARA_CONSIDERAR_VIVO = 150
INTERVALO_VIGILANCIA_S = 60.0


SEGUNDOS_PARA_CONSIDERAR_VIVO = 150
INTERVALO_VIGILANCIA_S = 60.0


class Vigia:
    """Observa o lote e transforma em e-mail o que precisa de gente.

    Roda no laço principal, fora dos workers: assim continua olhando mesmo
    quando todos os órgãos estão parados — que é justamente quando alguém
    precisa ser avisado.
    """

    def __init__(self, cfg: Config, orgaos: list[ConfigOrgao]) -> None:
        self.cfg = cfg
        self.orgaos = orgaos
        self._proxima = 0.0
        self._falhas_vistas_ate = tempo.agora_iso()

    def rodada(self, conn) -> None:
        if time.monotonic() < self._proxima:
            return
        self._proxima = time.monotonic() + INTERVALO_VIGILANCIA_S
        try:
            for orgao in self.orgaos:
                self._recuperar_falhas(conn, orgao)
            self._avisar_lotes_concluidos(conn)
            for orgao in self.orgaos:
                self._avisar_travamento(conn, orgao)
            self._avisar_falhas_definitivas(conn)
        except Exception:
            log.exception("falha_na_vigilancia")

    # ------------------------------------------------------------------
    def _recuperar_falhas(self, conn, orgao: ConfigOrgao) -> None:
        """Devolve as falhas à fila quando não há mais nada a fazer.

        Roda só com a fila vazia: enquanto houver item pendente, o lote
        ainda está andando e reenfileirar agora só atrapalharia a ordem.
        Ver core/recuperacao.py para o porquê da espera crescente.
        """
        p = orgao.recuperacao
        if not p.ativa:
            return

        falhados = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE orgao = ? AND status = ?",
            (orgao.codigo, Status.FAILED),
        ).fetchone()["n"]

        if not falhados:
            # Lote fechado: a contagem recomeça, senão o próximo herdaria a
            # espera de 6h da última rodada deste.
            recuperacao.zerar(conn, orgao.codigo)
            return

        if fila.ha_trabalho(conn, orgao.codigo):
            return

        estado_atual = recuperacao.agendar(conn, orgao.codigo, p)
        if not recuperacao.pode_recuperar(conn, orgao.codigo):
            log.info("recuperacao_agendada", extra={
                "orgao": orgao.codigo,
                "falhados": falhados,
                "rodada": estado_atual.rodadas + 1,
                "em": estado_atual.proxima_em,
            })
            return

        devolvidos = fila.reenfileirar_falhados(conn, orgao.codigo)
        novo = recuperacao.registrar_rodada(conn, orgao.codigo, p)
        log.warning("recuperacao_automatica", extra={
            "orgao": orgao.codigo,
            "devolvidos": devolvidos,
            "rodada": novo.rodadas,
            "proxima_em": novo.proxima_em,
        })

        if novo.rodadas == p.avisar_apos:
            # Uma vez só, na rodada exata: o robô continua tentando, mas a
            # essa altura o problema não é passageiro e alguém precisa ver.
            alertas.enviar(
                self.cfg.alertas,
                f"{orgao.codigo}: {devolvidos} item(ns) resistindo",
                f"Já são {novo.rodadas} rodadas de recuperação automática e "
                f"estes itens continuam sem resposta do portal.",
                acao="1. Abrir o painel em Itens > Situação: Falhou.\n"
                     "2. Conferir a coluna Retorno do portal — se a mensagem "
                     "for sempre a mesma, pode ser tela que o robô ainda não "
                     "conhece.\n"
                     "3. O robô NÃO desistiu: segue tentando com intervalo "
                     "cada vez maior.",
                dados={"Itens": str(devolvidos), "Rodadas": str(novo.rodadas)},
                acoes=self._link_das_falhas(),
                severidade="aviso",
                chave=f"recuperacao:{orgao.codigo}",
            )

    # ------------------------------------------------------------------
    def _avisar_lotes_concluidos(self, conn) -> None:
        for resumo in vigilancia.lotes_recem_concluidos(conn):
            log.info("lote_concluido", extra={"lote": resumo.lote_id,
                                              "total": resumo.total})
            alertas.enviar(
                self.cfg.alertas,
                f"Lote #{resumo.lote_id} concluído",
                f"{resumo.com_certidao} certidões obtidas de {resumo.total} "
                f"empresas.",
                acao=self._acao_do_lote(resumo),
                dados=resumo.como_campos(),
                acoes=self._links_do_lote(resumo.lote_id),
                severidade="aviso" if (resumo.falhados or resumo.sem_certidao)
                            else "ok",
            )

    def _acao_do_lote(self, resumo) -> str:
        """O que a pessoa faz agora que o lote terminou."""
        passos = ["1. Baixar a planilha e os PDFs pelos botões abaixo.",
                  "2. Enviar as certidões aos clientes."]
        proximo = 3
        if resumo.sem_certidao:
            passos.append(f"{proximo}. Tratar as {resumo.sem_certidao} empresas "
                          f"sem certidão (abas Positivas e Pendencia manual "
                          f"da planilha) — essas exigem regularização ou "
                          f"atendimento no e-CAC.")
            proximo += 1
        if resumo.falhados:
            passos.append(f"{proximo}. Conferir os {resumo.falhados} itens que "
                          f"não concluíram (aba Erros) e reenviar pelo painel "
                          f"se for problema passageiro.")
        return "\n".join(passos)

    def _links_do_lote(self, lote_id: int) -> list[tuple[str, str]]:
        """Botões para baixar a planilha e os PDFs.

        O Teams não aceita anexo, e o pacote de PDFs (~150 MB num lote
        completo) não caberia em e-mail de qualquer forma. Link tem outra
        vantagem: o arquivo é gerado no clique, sempre atualizado — anexo
        congela no momento do envio.
        """
        base = (self.cfg.alertas.url_painel or "").rstrip("/")
        if not base:
            return []
        # A planilha é do lote (o que aquela importação produziu); o pacote
        # de certidões é do mês, que é o corte que o cliente recebe.
        mes = tempo.agora_iso()[:7]
        return [("Baixar planilha", f"{base}/relatorio/{mes}.xlsx"),
                ("Baixar certidões do mês", f"{base}/certidoes/{mes}.zip"),
                ("Abrir painel", base)]

    def _avisar_travamento(self, conn, orgao: ConfigOrgao) -> None:
        """Fila com trabalho e nada concluindo = travado, mesmo com o
        processo vivo. O heartbeat sozinho não pega este caso."""
        parado_ha = vigilancia.minutos_sem_progresso(conn, orgao.codigo)
        chave = f"travado:{orgao.codigo}"

        if parado_ha is None or parado_ha < vigilancia.MINUTOS_SEM_PROGRESSO:
            alertas.fechar_incidente(
                self.cfg.alertas, chave,
                f"{orgao.codigo} normalizado",
                "O robô voltou a concluir consultas.",
                severidade="ok",
            )
            return

        estado = breaker.consultar(conn, orgao.codigo)
        log.warning("sem_progresso", extra={"orgao": orgao.codigo,
                                            "minutos": round(parado_ha)})

        if estado.estado == breaker.FECHADO:
            o_que_fazer = (
                "Acessar o servidor e verificar, nesta ordem:\n"
                "1. A janela do Edge está aberta e maximizada?\n"
                "2. Alguém está usando o computador? O robô precisa da tela "
                "só para ele.\n"
                "3. Há alguma janela por cima do navegador?\n"
                "Se tudo estiver certo, reiniciar com `cnd rodar --forcar`."
            )
        else:
            o_que_fazer = ("Nada agora — o robô está de castigo e retoma "
                           "sozinho. Se passar de 2h assim, avise.")

        pendentes = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE orgao = ? AND status IN "
            "('PENDING','RETRY_WAIT')", (orgao.codigo,)).fetchone()["n"]

        alertas.abrir_incidente(
            self.cfg.alertas, chave,
            f"{orgao.codigo} parado há {parado_ha:.0f} min",
            "Há itens na fila, mas nenhuma consulta conclui.",
            acao=o_que_fazer,
            dados={
                "Itens esperando": f"{pendentes:,}".replace(",", "."),
                "Disjuntor": ("fechado (deveria estar trabalhando)"
                              if estado.estado == breaker.FECHADO
                              else f"{estado.estado.lower()}"),
                **({"Motivo da pausa": estado.motivo} if estado.motivo else {}),
                **({"Retoma às": estado.aberto_ate[11:19]}
                   if estado.aberto_ate else {}),
            },
            acoes=self._link_painel(),
            severidade="erro",
        )

    def _link_painel(self) -> list[tuple[str, str]]:
        base = (self.cfg.alertas.url_painel or "").rstrip("/")
        return [("Abrir painel", base)] if base else []

    def _avisar_falhas_definitivas(self, conn) -> None:
        """Resumo, não um e-mail por item: 200 avisos de falha viram ruído,
        e ruído faz a caixa de entrada ser ignorada no dia que importa."""
        marca = self._falhas_vistas_ate
        novas: list = []
        for orgao in self.orgaos:
            novas.extend(vigilancia.falhas_definitivas(conn, orgao.codigo, marca))

        if not novas:
            return

        self._falhas_vistas_ate = tempo.agora_iso()
        log.warning("falhas_definitivas", extra={"quantidade": len(novas)})
        alertas.enviar(
            self.cfg.alertas,
            f"{len(novas)} item(ns) não concluíram",
            "Tentaram 3 vezes e o portal não devolveu certidão.",
            acao="1. Abrir o painel em Itens > filtrar Situação: Falhou.\n"
                 "2. Conferir a coluna Retorno do portal — normalmente é "
                 "empresa que exige atendimento no e-CAC.\n"
                 "3. Se for problema passageiro, clicar em Reenviar itens "
                 "com falha.",
            dados=vigilancia.campos_das_falhas(novas),
            acoes=self._link_das_falhas(),
            severidade="aviso",
            chave="falhas",
        )

    def _link_das_falhas(self) -> list[tuple[str, str]]:
        base = (self.cfg.alertas.url_painel or "").rstrip("/")
        return [("Ver no painel", f"{base}/jobs?status=FAILED")] if base else []
