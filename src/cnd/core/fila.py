"""A fila de trabalho e a máquina de estados do job.

A fila é a própria tabela `job`: não existe broker de mensagens.
Um worker "reivindica" o próximo job com uma transação exclusiva, o que
garante que dois workers nunca peguem o mesmo item — ver ADR-002.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Collection
from datetime import date

from cnd.core import controle, tempo
from cnd.core.modelos import (
    COM_PDF,
    CONCLUSIVOS,
    Desfecho,
    Documento,
    JobReivindicado,
    ResultadoTentativa,
    Status,
)

# --------------------------------------------------------------------------
# Tirar trabalho da fila
# --------------------------------------------------------------------------

def reivindicar(conn: sqlite3.Connection, orgao: str,
                impedidos: Collection[str] = ()) -> JobReivindicado | None:
    """Pega o próximo job disponível do órgão e marca como RUNNING.

    Vale para item novo (`PENDING`) e retry cuja espera já venceu
    (`RETRY_WAIT`). Devolve None se não houver nada disponível agora.

    UMA PLANILHA POR VEZ. Só entram itens da planilha da vez — a mais
    antiga que tem item para agora, ou a que foi mandada "Rodar agora" (ver
    `lote_da_vez`). Sem isso, cada automação puxava o item mais antigo de
    QUALQUER planilha ativa, e três envios andavam misturados: a tela
    mostrava uma planilha, o robô emitia de outra, e nenhuma terminava
    (22/09/2026). As demais esperam, e o robô só passa à seguinte quando a
    da vez não tem mais o que entregar agora.

    `impedidos` são os órgãos parados neste instante (disjuntor, janela de
    horário, adapter que não subiu): a planilha deles não segura a vez —
    ver `lote_da_vez`. Quem pergunta sai da lista antes da consulta, porque
    um worker não pode impedir a si mesmo: ele só chega aqui podendo
    trabalhar, e outro worker do mesmo órgão pode tê-lo marcado.

    Planilha estacionada ou cancelada NAQUELA automação não é entregue —
    ver core/controle.py. A conferência entra aqui, dentro da mesma
    transação que reivindica, e não numa checagem antes: entre um SELECT
    de fora e o UPDATE daqui caberia outro worker.

    O BEGIN IMMEDIATE trava a escrita já na abertura da transação: é isso
    que impede dois workers de selecionarem a mesma linha antes de qualquer
    um marcar RUNNING.
    """
    agora = tempo.agora_iso()
    corte, parados = _sem_os_impedidos(
        [o for o in impedidos if o != orgao], alias="j2")
    conn.execute("BEGIN IMMEDIATE")
    try:
        linha = conn.execute(
            f"""
            SELECT j.id, j.lote_id, j.tentativas, j.empresa_id,
                   e.documento, e.tipo_documento, e.nome, e.data_nascimento
              FROM job j
              JOIN empresa e ON e.id = j.empresa_id
              -- LEFT JOIN, e COALESCE no lugar do NULL: quase nenhuma
              -- planilha tem linha de controle, e exigir uma faria a fila
              -- inteira depender de um INSERT na importação.
              LEFT JOIN fila_controle c
                     ON c.lote_id = j.lote_id AND c.orgao = j.orgao
             WHERE j.orgao = ?
               AND j.status IN (?, ?)
               AND j.proxima_execucao_em <= ?
               AND COALESCE(c.situacao, ?) = ?
               -- UMA PLANILHA POR VEZ, decidida na mesma transação que
               -- reivindica: ver `lote_da_vez`.
               AND j.lote_id = COALESCE(
                   -- Planilha com item na mão de um worker segura a vez:
                   -- sem isto, o último item dela ainda sendo emitido já
                   -- deixava a seguinte começar, que é a mistura que se
                   -- quer evitar.
                   (SELECT j0.lote_id FROM job j0
                     WHERE j0.status = ? ORDER BY j0.lote_id LIMIT 1),
                   (SELECT j2.lote_id
                     FROM job j2
                     LEFT JOIN fila_controle c2
                            ON c2.lote_id = j2.lote_id AND c2.orgao = j2.orgao
                    WHERE j2.status IN (?, ?)
                      AND j2.proxima_execucao_em <= ?
                      AND COALESCE(c2.situacao, ?) = ?{corte}
                    ORDER BY COALESCE(c2.prioridade, 0) DESC, j2.lote_id
                    LIMIT 1)
               )
             -- Prioridade primeiro: é assim que "Rodar agora" fura a fila
             -- sem mexer nos ids, que guardam a ordem de chegada.
             ORDER BY COALESCE(c.prioridade, 0) DESC,
                      j.proxima_execucao_em, j.id
             LIMIT 1
            """,
            (orgao, Status.PENDING, Status.RETRY_WAIT, agora,
             controle.ATIVA, controle.ATIVA,
             Status.RUNNING,
             Status.PENDING, Status.RETRY_WAIT, agora,
             controle.ATIVA, controle.ATIVA, *parados),
        ).fetchone()

        if linha is None:
            conn.execute("COMMIT")
            return None

        conn.execute(
            "UPDATE job SET status = ?, atualizado_em = ? WHERE id = ?",
            (Status.RUNNING, agora, linha["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return JobReivindicado(
        job_id=linha["id"],
        lote_id=linha["lote_id"],
        orgao=orgao,
        tentativas=linha["tentativas"],
        doc=Documento(
            empresa_id=linha["empresa_id"],
            documento=linha["documento"],
            tipo=linha["tipo_documento"],
            nome=linha["nome"],
            lote_id=linha["lote_id"],
            data_nascimento=(date.fromisoformat(linha["data_nascimento"])
                             if linha["data_nascimento"] else None),
        ),
    )


def _sem_os_impedidos(impedidos: Collection[str],
                      alias: str = "j") -> tuple[str, list]:
    """O `AND ...` que tira os órgãos parados da escolha, e seus valores.

    Vazio quando não há nenhum, que é o caso comum — e aí a consulta sai
    exatamente como era. `alias` é o nome da tabela job na consulta que
    chama: dentro de `reivindicar` a subconsulta da vez usa outro.
    """
    if not impedidos:
        return "", []
    marcas = ", ".join("?" * len(impedidos))
    return f" AND {alias}.orgao NOT IN ({marcas})", list(impedidos)


def lote_da_vez(conn: sqlite3.Connection,
                impedidos: Collection[str] = ()) -> int | None:
    """Qual planilha o robô está autorizado a emitir AGORA.

    Uma de cada vez, e a mais antiga primeiro: a planilha que chegou antes
    termina antes. "Rodar agora" (prioridade, ver core/controle.py) passa na
    frente sem mexer nos ids, que são a memória da ordem de chegada.

    Planilha com item NA MÃO de um worker segura a vez: o último item dela
    ainda sendo emitido não pode deixar a seguinte começar. RUNNING órfão de
    um crash não trava nada, porque a subida do orquestrador devolve todos
    eles à fila (`recuperar_orfaos`).

    Fora isso, "agora" é literal: entra só planilha com item que o robô pode
    pegar neste instante. Se a mais antiga está inteira em espera de
    retentativa, a seguinte usa a vez nesse meio-tempo — parar a máquina
    para esperar uma espera seria trocar mistura por ociosidade. É a mesma
    regra da vez da tela (orquestrador/vez_da_tela.py).

    `impedidos` são os órgãos que não conseguem trabalhar agora: disjuntor
    aberto, fora da janela de horário, adapter que não subiu. Item deles não
    dá a vez a planilha nenhuma, pelo mesmo motivo — senão a planilha cujo
    único órgão está de castigo segurava a máquina INTEIRA, e o SEFAZ-ES
    ficava parado a noite toda esperando a janela do RFB abrir (23/09/2026).
    Item já em curso continua segurando, impedido ou não: ele vai terminar.
    Quem pergunta nunca está na lista — ver `reivindicar`.

    A tela usa isto para dizer qual planilha está valendo e quais esperam; a
    fila usa a MESMA consulta dentro da transação que reivindica, porque
    entre um SELECT de fora e o UPDATE caberia outro worker.
    """
    agora = tempo.agora_iso()
    em_curso = conn.execute(
        "SELECT lote_id FROM job WHERE status = ? ORDER BY lote_id LIMIT 1",
        (Status.RUNNING,),
    ).fetchone()
    if em_curso is not None:
        return em_curso["lote_id"]

    corte, parados = _sem_os_impedidos(impedidos)
    linha = conn.execute(
        f"""
        SELECT j.lote_id
          FROM job j
          LEFT JOIN fila_controle c
                 ON c.lote_id = j.lote_id AND c.orgao = j.orgao
         WHERE j.status IN (?, ?)
           AND j.proxima_execucao_em <= ?
           AND COALESCE(c.situacao, ?) = ?{corte}
         ORDER BY COALESCE(c.prioridade, 0) DESC, j.lote_id
         LIMIT 1
        """,
        (Status.PENDING, Status.RETRY_WAIT, agora, controle.ATIVA,
         controle.ATIVA, *parados),
    ).fetchone()
    return linha["lote_id"] if linha is not None else None


def esperando_a_vez(conn: sqlite3.Connection, orgao: str,
                    impedidos: Collection[str] = ()) -> bool:
    """Este órgão tem trabalho, mas só em planilha que não é a da vez.

    Não é travamento nem fim de serviço: é a fila respeitando "uma planilha
    por vez". O vigia precisa saber disso — sem essa pergunta, um envio
    grande segurando a vez por meia hora faria o aviso de "travado" sair
    para todas as outras automações, que é alarme falso pelo mesmo motivo
    da vez da tela (orquestrador/vez_da_tela.py).
    """
    vez = lote_da_vez(conn, impedidos)
    if vez is None:
        return False

    linha = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM job j
          LEFT JOIN fila_controle c
                 ON c.lote_id = j.lote_id AND c.orgao = j.orgao
         WHERE j.orgao = ?
           AND j.lote_id = ?
           AND (j.status = ?
                OR (j.status IN (?, ?) AND COALESCE(c.situacao, ?) = ?))
        """,
        (orgao, vez, Status.RUNNING, Status.PENDING, Status.RETRY_WAIT,
         controle.ATIVA, controle.ATIVA),
    ).fetchone()
    return linha["n"] == 0 and ha_trabalho(conn, orgao)


def ha_trabalho(conn: sqlite3.Connection, orgao: str) -> bool:
    """Existe algo que o robô AINDA VAI pegar neste órgão?

    Item de planilha estacionada ou cancelada não conta: `reivindicar` nunca
    o entrega. Contado, o robô ficava de pé sem fazer nada, esperando um
    trabalho que não vinha (17/09/2026). O que já está em RUNNING conta
    sempre — estacionar não interrompe o item em andamento.
    """
    linha = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM job j
          LEFT JOIN fila_controle c
                 ON c.lote_id = j.lote_id AND c.orgao = j.orgao
         WHERE j.orgao = ?
           AND (j.status = ?
                OR (j.status IN (?, ?) AND COALESCE(c.situacao, ?) = ?))
        """,
        (orgao, Status.RUNNING, Status.PENDING, Status.RETRY_WAIT,
         controle.ATIVA, controle.ATIVA),
    ).fetchone()
    return linha["n"] > 0


def falhas_em_filas_ativas(conn: sqlite3.Connection, orgao: str) -> int:
    """Itens FAILED que a recuperação automática ainda vai devolver.

    Só de planilha ativa: a estacionada ou cancelada não é devolvida
    (`reenfileirar_falhados(somente_ativas=True)`), então segurar o robô de
    pé por causa dela seria esperar o que não vem.
    """
    return conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM job j
          LEFT JOIN fila_controle c
                 ON c.lote_id = j.lote_id AND c.orgao = j.orgao
         WHERE j.orgao = ? AND j.status = ?
           AND COALESCE(c.situacao, ?) = ?
        """,
        (orgao, Status.FAILED, controle.ATIVA, controle.ATIVA),
    ).fetchone()["n"]


def ordem_na_fila(conn: sqlite3.Connection, orgao: str,
                  impedidos: Collection[str] = ()) -> tuple[int, int, int] | None:
    """Onde a fila deste órgão está na fila de TODAS as automações de tela.

    None quando não há item para pegar AGORA: nada pendente, só
    retentativas agendadas para depois, planilha estacionada/cancelada —
    ou trabalho que existe, mas só em planilha que não é a da vez.

    Só a planilha DA VEZ disputa a tela, pela mesma razão que só ela é
    reivindicada. Sem esse corte a tela ia para quem não conseguia pegar
    item nenhum: o dono anunciado ouvia `reivindicar` devolver None e
    voltava a esperar, de navegador aberto e sem emitir nada.

    A ordem é a da chegada das planilhas, com "Rodar agora" na frente —
    a mesma que `reivindicar` usa dentro de um órgão, estendida entre os
    órgãos. Menor vem primeiro. Ver orquestrador/vez_da_tela.py.
    """
    vez = lote_da_vez(conn, [o for o in impedidos if o != orgao])
    if vez is None:
        return None

    linha = conn.execute(
        """
        SELECT MAX(COALESCE(c.prioridade, 0)) AS prioridade,
               MIN(j.lote_id) AS lote, MIN(j.id) AS primeiro
          FROM job j
          LEFT JOIN fila_controle c
                 ON c.lote_id = j.lote_id AND c.orgao = j.orgao
         WHERE j.orgao = ?
           AND j.lote_id = ?
           AND j.status IN (?, ?)
           AND j.proxima_execucao_em <= ?
           AND COALESCE(c.situacao, ?) = ?
        """,
        (orgao, vez, Status.PENDING, Status.RETRY_WAIT, tempo.agora_iso(),
         controle.ATIVA, controle.ATIVA),
    ).fetchone()
    if linha is None or linha["primeiro"] is None:
        return None
    return (-int(linha["prioridade"]), int(linha["lote"]), int(linha["primeiro"]))


# --------------------------------------------------------------------------
# Registrar o que aconteceu
# --------------------------------------------------------------------------

def abrir_tentativa(conn: sqlite3.Connection, job: JobReivindicado, worker: int) -> int:
    """Cria a linha de tentativa e devolve o id dela."""
    cursor = conn.execute(
        "INSERT INTO tentativa (job_id, numero, iniciada_em, worker) VALUES (?, ?, ?, ?)",
        (job.job_id, job.tentativas + 1, tempo.agora_iso(), worker),
    )
    return int(cursor.lastrowid)


def fechar_tentativa(conn: sqlite3.Connection, tentativa_id: int,
                     resultado: ResultadoTentativa) -> None:
    conn.execute(
        """
        UPDATE tentativa
           SET finalizada_em = ?, desfecho = ?, mensagem_portal = ?, evidencia = ?
         WHERE id = ?
        """,
        (
            tempo.agora_iso(),
            str(resultado.desfecho),
            resultado.mensagem_portal,
            str(resultado.evidencia) if resultado.evidencia else None,
            tentativa_id,
        ),
    )


def concluir(conn: sqlite3.Connection, job: JobReivindicado,
             resultado: ResultadoTentativa) -> None:
    """Fecha o job com um desfecho definitivo e guarda a certidão, se houver."""
    if resultado.desfecho not in CONCLUSIVOS:
        raise ValueError(f"{resultado.desfecho} não é um desfecho conclusivo")

    agora = tempo.agora_iso()
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            UPDATE job
               SET status = ?, desfecho = ?, tentativas = tentativas + 1, atualizado_em = ?
             WHERE id = ?
            """,
            (Status.DONE, str(resultado.desfecho), agora, job.job_id),
        )

        if resultado.desfecho in COM_PDF and resultado.caminho_pdf is not None:
            from cnd.infra.arquivos import hash_arquivo

            conn.execute(
                """
                INSERT INTO certidao
                    (job_id, tipo, emitida_em, valida_ate, codigo_controle, caminho_pdf, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    str(resultado.desfecho),
                    agora,
                    resultado.validade.isoformat() if resultado.validade else None,
                    resultado.codigo_controle,
                    str(resultado.caminho_pdf),
                    hash_arquivo(resultado.caminho_pdf),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reagendar(conn: sqlite3.Connection, job: JobReivindicado,
              desfecho: Desfecho, espera_s: float) -> None:
    """Devolve o job para a fila, para nova tentativa daqui a `espera_s`."""
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = NULL, tentativas = tentativas + 1,
               proxima_execucao_em = ?, atualizado_em = ?
         WHERE id = ?
        """,
        (Status.RETRY_WAIT, tempo.daqui_a(espera_s), tempo.agora_iso(), job.job_id),
    )


def devolver(conn: sqlite3.Connection, job: JobReivindicado) -> None:
    """Devolve um job reivindicado sem contar tentativa.

    Usado quando o worker para antes de abrir a tentativa de verdade. Parada
    limpa não pode aparecer como erro técnico nem consumir uma das três chances
    do item.
    """
    agora = tempo.agora_iso()
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = NULL, proxima_execucao_em = ?, atualizado_em = ?
         WHERE id = ?
        """,
        (Status.PENDING, agora, agora, job.job_id),
    )


def falhar(conn: sqlite3.Connection, job: JobReivindicado, desfecho: Desfecho) -> None:
    """Esgotou as tentativas. Terminal para o sistema, reenfileirável pelo painel."""
    conn.execute(
        """
        UPDATE job
           SET status = ?, desfecho = ?, tentativas = tentativas + 1, atualizado_em = ?
         WHERE id = ?
        """,
        (Status.FAILED, str(desfecho), tempo.agora_iso(), job.job_id),
    )


# --------------------------------------------------------------------------
# Manutenção
# --------------------------------------------------------------------------

def recuperar_orfaos(conn: sqlite3.Connection) -> int:
    """Jobs que ficaram RUNNING por causa de um crash voltam para a fila.

    Chamado na subida do orquestrador (RNF-08). Como o orquestrador é o
    único que marca RUNNING, qualquer RUNNING encontrado na subida é órfão.
    """
    cursor = conn.execute(
        "UPDATE job SET status = ?, atualizado_em = ? WHERE status = ?",
        (Status.PENDING, tempo.agora_iso(), Status.RUNNING),
    )
    return cursor.rowcount


# Como os adapters descrevem uma consulta que NÃO produziu resposta do órgão
# sobre a empresa. Item encerrado com uma destas mensagens é consulta
# perdida, e volta para a fila junto com os que falharam.
#
#   · "não foi possível ler a tela" — o robô cego não leu o resultado;
#   · "código da imagem inválido"   — o portal do MA recusou a LEITURA do
#     captcha. Chegou a fechar 17 itens como pendência manual em 16/09/2026,
#     e pendência que não existe ninguém tem como tratar.
MENSAGEM_TELA_ILEGIVEL = "não foi possível ler a tela"
MENSAGEM_CAPTCHA_RECUSADO = "código da imagem inválido"
MENSAGENS_SEM_RESPOSTA = (MENSAGEM_TELA_ILEGIVEL, MENSAGEM_CAPTCHA_RECUSADO)


def reenfileirar_falhados(
    conn: sqlite3.Connection, orgao: str | None = None,
    lote_id: int | None = None, somente_ativas: bool = False,
) -> int:
    """Devolve para a fila o que não teve resposta, zerando as tentativas.

    São dois grupos, e os dois são a mesma coisa para quem opera: consulta
    que não produziu certidão nem recado do órgão.

      · jobs FAILED — esgotaram as três tentativas;
      · jobs encerrados como pendência manual só porque o robô não
        conseguiu ler a tela. Até 15/08/2026 esse caso era conclusivo, e um
        lote inteiro parou lá quando o portal passou a devolver 400 do
        nginx (ver adapters/federal/rfb/cego.py). Eles ficariam para sempre no
        relatório como pendência que ninguém tem como tratar — o e-CAC não
        mostra nada, porque nunca houve pendência.

    Usado pelo painel depois que a causa da falha foi corrigida.

    `somente_ativas` deixa de fora as planilhas estacionadas e canceladas.
    É o que a recuperação automática usa: planilha parada por decisão de
    quem opera não volta a andar sozinha.
    """
    agora = tempo.agora_iso()
    filtros: list[str] = []
    filtros_args: list = []
    if somente_ativas:
        filtros.append(
            "NOT EXISTS (SELECT 1 FROM fila_controle c "
            "WHERE c.lote_id = job.lote_id AND c.orgao = job.orgao "
            "AND c.situacao <> ?)")
        filtros_args.append(controle.ATIVA)
    if orgao:
        filtros.append("orgao = ?")
        filtros_args.append(orgao)
    if lote_id:
        filtros.append("lote_id = ?")
        filtros_args.append(lote_id)
    recorte = "".join(f" AND {condicao}" for condicao in filtros)

    cursor = conn.execute(
        f"""
        UPDATE job SET status = ?, desfecho = NULL, tentativas = 0,
                       proxima_execucao_em = ?, atualizado_em = ?
         WHERE status = ?{recorte}
        """,
        (Status.PENDING, agora, agora, Status.FAILED, *filtros_args),
    )
    total = cursor.rowcount

    condicoes = " OR ".join(
        "mensagem_portal LIKE ?" for _ in MENSAGENS_SEM_RESPOSTA)
    cursor = conn.execute(
        f"""
        UPDATE job SET status = ?, desfecho = NULL, tentativas = 0,
                       proxima_execucao_em = ?, atualizado_em = ?
         WHERE status = ? AND desfecho = ?{recorte}
           AND id IN (SELECT job_id FROM tentativa
                       WHERE {condicoes})
        """,
        (Status.PENDING, agora, agora, Status.DONE,
         str(Desfecho.PENDENCIA_MANUAL), *filtros_args,
         *(f"%{mensagem}%" for mensagem in MENSAGENS_SEM_RESPOSTA)),
    )
    return total + cursor.rowcount


def antecipar_esperas(
    conn: sqlite3.Connection, orgao: str | None = None,
    lote_id: int | None = None,
) -> int:
    """Traz para agora os itens que estão só esperando o relógio.

    Um item em RETRY_WAIT não tem problema nenhum: ele já foi reagendado e
    só aguarda a hora marcada. Mas, enquanto espera, o robô pode ficar
    ocioso com a fila vazia — e não havia como intervir: "Tentar de novo"
    só alcança quem está em FAILED. Foi o que aconteceu em 17/08/2026, com
    13 itens marcados para dali a 20 minutos e o robô parado olhando.

    Não zera as tentativas de propósito: quem pede isto quer adiantar a
    fila, não dar chances extras. As três continuam valendo, e o disjuntor
    continua protegendo o portal se a pressa não tiver sido boa ideia.
    """
    agora = tempo.agora_iso()
    filtros: list[str] = []
    filtros_args: list = []
    if orgao:
        filtros.append("orgao = ?")
        filtros_args.append(orgao)
    if lote_id:
        filtros.append("lote_id = ?")
        filtros_args.append(lote_id)
    recorte = "".join(f" AND {condicao}" for condicao in filtros)

    cursor = conn.execute(
        f"""
        UPDATE job SET proxima_execucao_em = ?, atualizado_em = ?
         WHERE status = ?{recorte} AND proxima_execucao_em > ?
        """,
        (agora, agora, Status.RETRY_WAIT, *filtros_args, agora),
    )
    return cursor.rowcount


def certidao_do_mes(conn: sqlite3.Connection, empresa_id: int, orgao: str,
                    lote_id: int | None = None) -> sqlite3.Row | None:
    """Certidão desta empresa/órgão emitida no MÊS CORRENTE (RNF-04).

    O critério é a data de emissão, não a validade — e a diferença importa.
    A certidão da Receita vale 180 dias, mas quem recebe (bancos, licitações,
    tomadores) exige emissão do mês corrente. Reaproveitar uma certidão ainda
    válida porém do mês passado entregaria um documento que seria recusado,
    e o job apareceria como concluído: erro silencioso, o pior tipo.

    Vale, portanto, só dentro do mesmo mês: em 03/09 aproveita a de 01/09,
    mas não a de 31/08.

    E só DENTRO DA MESMA PLANILHA. Duas planilhas são trabalhos separados,
    ainda que tragam os mesmos CNPJs: quem manda a mesma lista de novo está
    pedindo certidões novas, não um relatório de que já existem. Sem esse
    filtro, a segunda remessa fechava inteira como APROVEITADA, sem PDF
    novo — regra definida pela operação em 14/08/2026.
    """
    filtro = "AND j.lote_id = ?" if lote_id is not None else ""
    args: tuple = ((empresa_id, orgao, lote_id) if lote_id is not None
                   else (empresa_id, orgao))
    return conn.execute(
        f"""
        SELECT c.*
          FROM certidao c
          JOIN job j ON j.id = c.job_id
         WHERE j.empresa_id = ?
           AND j.orgao = ?
           {filtro}
           AND strftime('%Y-%m', c.emitida_em) = strftime('%Y-%m', 'now')
         ORDER BY c.emitida_em DESC
         LIMIT 1
        """,
        args,
    ).fetchone()
