"""Linha de comando do CND Bot.

    cnd importar CND_MIA_0726.xlsx --abas RFB
    cnd rodar
    cnd painel
    cnd relatorio 1
    cnd simular 200          # lote falso, para exercitar o sistema offline
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _importar(args) -> int:
    from cnd.infra.db import conectar, criar_schema
    from cnd.ingestao.planilha import importar

    # Arquivo no lugar errado é o erro mais comum aqui, e é previsível.
    # Sem esta checagem, o openpyxl estoura com um traceback de Python na
    # cara de quem só queria importar uma planilha — e o rastro nem sequer
    # diz qual caminho ele procurou.
    if not args.planilha.exists():
        print(f"\n  NAO ENCONTREI A PLANILHA:\n    {args.planilha.resolve()}\n")
        print("  Copie o arquivo para a pasta do ACTA, ou passe o caminho")
        print("  completo entre aspas. Exemplo:\n")
        print('    cnd importar "C:\\Users\\...\\Downloads\\planilha.xlsx"'
              " --abas RFB\n")
        return 1
    if args.planilha.suffix.lower() not in (".xlsx", ".xlsm"):
        print(f"\n  {args.planilha.name} nao e uma planilha do Excel.\n")
        return 1

    conn = conectar()
    criar_schema(conn)
    descricao = args.descricao or f"Importação de {args.planilha.name}"
    abas = [a.upper() for a in args.abas] if args.abas else None
    lote_id, leitura = importar(conn, args.planilha, descricao, abas)

    print(f"\nLote #{lote_id} criado a partir de {args.planilha.name}")
    print(f"  jobs criados : {len(leitura.itens)}")
    print(f"  rejeitados   : {len(leitura.rejeitados)}")
    for r in leitura.rejeitados[:10]:
        print(f"    [{r.aba} linha {r.linha}] {r.valor_original!r}: {r.motivo}")
    if len(leitura.rejeitados) > 10:
        print(f"    ... e mais {len(leitura.rejeitados) - 10}")
    conn.close()
    return 0


def _zerar(args) -> int:
    """Apaga o trabalho inteiro desta máquina. Não tem desfazer."""
    from cnd.infra import limpeza
    from cnd.infra.config import carregar
    from cnd.infra.db import conectar, criar_schema, garantir

    cfg = carregar(args.config)
    garantir(cfg.banco)

    if not args.sim:
        print("\n  Isto apaga TUDO desta máquina: planilhas, itens,")
        print("  tentativas e os PDFs já emitidos. Não tem desfazer.")
        print("  Calibragem, config.toml e logs ficam.\n")
        print("  Se é isso mesmo, repita com --sim:\n")
        print("    cnd zerar --sim\n")
        return 1

    conn = conectar(cfg.banco)
    try:
        criar_schema(conn)
        resultado = limpeza.zerar(conn, (cfg.pasta_certidoes,
                                         cfg.pasta_evidencias))
    finally:
        conn.close()

    print(f"\nMáquina zerada: {resultado.como_texto()}\n")
    return 0


def _rodar(args) -> int:
    from cnd.core import breaker
    from cnd.infra.config import carregar
    from cnd.infra.db import conectar, garantir
    from cnd.orquestrador.loop import executar

    cfg = carregar(args.config)
    garantir(cfg.banco)

    if args.reiniciar_ritmo:
        # O ritmo e o disjuntor guardam o que o robô aprendeu sobre o portal.
        # Ao trocar de adapter, esse aprendizado não vale mais: foi medido
        # com outra forma de acessar o site.
        conn = conectar(cfg.banco)
        for orgao in cfg.ativos():
            conn.execute("DELETE FROM ritmo WHERE orgao = ?", (orgao.codigo,))
            breaker.fechar(conn, orgao.codigo)
            conn.execute("UPDATE breaker SET aberturas = 0 WHERE orgao = ?",
                         (orgao.codigo,))
            print(f"  ritmo e disjuntor de {orgao.codigo} reiniciados "
                  f"(volta a {orgao.pacing.intervalo_inicial_s}s)")
        conn.close()

    executar(cfg, ate_esvaziar=args.ate_esvaziar, limite=args.limite,
             forcar=args.forcar)
    return 0


def _painel(args) -> int:
    import uvicorn

    from cnd.infra.config import carregar
    from cnd.infra.db import garantir

    garantir(carregar().banco)
    if args.host != "127.0.0.1":
        print(f"  Painel acessível pela rede em http://{args.host}:{args.porta}")
        print("  Outras máquinas e o aplicativo vão consultar este endereço.")
    # O app vai como OBJETO, não como "cnd.web.app:app". Com o nome em
    # texto, o empacotador não enxerga a dependência e deixa o módulo de
    # fora — o executável sobe e morre em "Could not import module".
    from cnd.web.app import app as aplicacao

    uvicorn.run(aplicacao, host=args.host, port=args.porta,
                log_level="warning", http="h11", ws="none")
    return 0


def _inicio_automatico(args) -> int:
    """Liga ou desliga a subida do painel junto com o Windows."""
    from cnd.infra import inicializacao

    if args.remover:
        tirou = inicializacao.remover() | inicializacao.remover_do_boot()
        print("O painel não sobe mais sozinho." if tirou
              else "Não estava instalado — nada a fazer.")
        return 0

    if args.no_boot:
        try:
            inicializacao.instalar_no_boot()
        except (PermissionError, RuntimeError) as erro:
            print(f"Não deu para instalar: {erro}")
            return 1
        # A da Inicialização sobraria tentando subir um segundo painel na
        # mesma porta, e morrendo com "endereço já em uso" a cada logon.
        inicializacao.remover()
        print("Pronto. O painel sobe no boot, sem depender de logon.")
        print("A máquina fica visível no ACTA 24 horas.")
        print("\nVale a partir do próximo boot. Para valer agora:")
        print("  schtasks /Run /TN \"ACTA Painel\"")
        return 0

    atalho = inicializacao.instalar()
    print(f"Pronto. O painel vai subir sozinho a cada logon:\n  {atalho}")
    print("\nVale a partir do próximo logon. Para valer agora, suba o painel")
    print("uma vez à mão:  cnd painel --host 0.0.0.0")
    return 0


def _relatorio(args) -> int:
    from cnd.infra.db import conectar_leitura
    from cnd.web.relatorio import gerar

    conn = conectar_leitura()
    from cnd.web.relatorio import Recorte, mes_corrente

    destino = gerar(conn, Recorte(args.mes or mes_corrente(), args.orgao),
                    args.saida)
    conn.close()
    print(f"Relatório gerado: {destino}")
    return 0


def _app(args) -> int:
    from cnd.desktop.app import main as abrir

    return abrir()


def _testar_alerta(args) -> int:
    """Manda um e-mail de teste, para conferir o SMTP antes de precisar dele."""
    from cnd.infra import alertas
    from cnd.infra.config import carregar

    cfg = carregar(args.config).alertas
    print(f"  método       : {cfg.metodo}")
    if cfg.metodo == "teams":
        url = cfg.teams_webhook
        print(f"  webhook      : {url[:55] + '...' if url else '(vazio)'}")
        print("  credencial   : nenhuma (a própria URL autoriza)")
    elif cfg.metodo == "relay":
        print(f"  servidor     : {cfg.smtp_host or '(vazio)'}:{cfg.smtp_porta}")
        print("  autenticação : nenhuma (Direct Send)")
    elif cfg.metodo == "graph":
        print(f"  tenant       : {cfg.graph_tenant_id or '(vazio)'}")
        print(f"  aplicativo   : {cfg.graph_client_id or '(vazio)'}")
        print(f"  segredo      : {'definido' if cfg.graph_client_secret else '(vazio)'}")
    else:
        print(f"  servidor     : {cfg.smtp_host}:{cfg.smtp_porta}")
        print(f"  usuário      : {cfg.smtp_usuario or '(vazio)'}")
    if cfg.metodo != "teams":
        print(f"  remetente    : {cfg.remetente or cfg.smtp_usuario or '(vazio)'}")
        print(f"  destinatários: {', '.join(cfg.destinatarios) or '(nenhum)'}")
    print()

    if not cfg.habilitado:
        print("  ENVIO DESLIGADO — falta preencher em config.toml:")
        for campo in cfg.o_que_falta():
            print(f"    - {campo}")
        if cfg.metodo == "teams":
            print()
            print("  No Teams: canal > ... > Fluxos de trabalho >")
            print("  'Publicar no canal quando uma solicitação de webhook for recebida'.")
            print("  Você mesmo cria, em 2 minutos, sem passar pela TI.")
        if cfg.metodo == "graph":
            print()
            print("  O registro de aplicativo é feito pela TI no Entra ID.")
            print("  As instruções completas estão nos comentários do config.toml.")
        print()
        print("  Enquanto isso o robô funciona normalmente e os avisos")
        print("  continuam sendo gravados em data/logs/.")
        return 1

    enviado = alertas.enviar(
        cfg,
        "Teste de configuração",
        "",
        dados={
            "Avisos": "lote concluído, robô parado, robô travado, "
                      "órgão suspenso, itens com falha",
            "Repetição": "no máximo 1 por tipo a cada 30 min",
            "Teto": "20 avisos por hora",
        },
        severidade="ok",
        forcar=True,
    )
    onde = "no canal do Teams" if cfg.metodo == "teams" else "por e-mail"
    print(f"  AVISO ENVIADO {onde}." if enviado else
          "  FALHOU. Veja o motivo em data/logs/orquestrador.jsonl")
    return 0 if enviado else 1


def _calibrar(args) -> int:
    from cnd.adapters.calibragem import calibrar, conferir
    from cnd.infra.db import RAIZ_PROJETO

    destino = args.saida or (RAIZ_PROJETO / "data" / "calibragem" /
                             f"{args.orgao.lower()}.json")
    if args.conferir:
        conferir(destino, destino.with_name(f"{args.orgao.lower()}-conferencia.png"))
    else:
        calibrar(destino)
    return 0


def _simular(args) -> int:
    """Cria um lote de CNPJs válidos gerados na hora, no órgão FAKE.

    Serve para ver fila, ritmo adaptativo, circuit breaker e painel
    funcionando de ponta a ponta sem tocar em portal nenhum.
    """
    import random

    from cnd.core.documentos import validar_cnpj
    from cnd.core.modelos import Status
    from cnd.infra.db import conectar, criar_schema

    def cnpj_valido() -> str:
        from cnd.core.documentos import _dv_cnpj

        base = [random.randint(0, 9) for _ in range(12)]
        dv1 = _dv_cnpj(base)
        dv2 = _dv_cnpj([*base, dv1])
        return "".join(map(str, [*base, dv1, dv2]))

    conn = conectar()
    criar_schema(conn)
    conn.execute("BEGIN")
    cursor = conn.execute(
        "INSERT INTO lote (descricao, arquivo_origem) VALUES (?, ?)",
        (f"Simulação de {args.quantidade} itens", "simulado"),
    )
    lote_id = cursor.lastrowid

    criados = 0
    for i in range(args.quantidade):
        documento = validar_cnpj(cnpj_valido())
        conn.execute(
            "INSERT INTO empresa (documento, tipo_documento, nome) VALUES (?, 'CNPJ', ?) "
            "ON CONFLICT (documento) DO NOTHING",
            (documento, f"EMPRESA SIMULADA {i + 1:04d}"),
        )
        empresa_id = conn.execute(
            "SELECT id FROM empresa WHERE documento = ?", (documento,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO job (lote_id, empresa_id, orgao, status) VALUES (?, ?, 'FAKE', ?) "
            "ON CONFLICT (lote_id, empresa_id, orgao) DO NOTHING",
            (lote_id, empresa_id, Status.PENDING),
        )
        criados += 1
    conn.execute("COMMIT")
    conn.close()

    print(f"Lote #{lote_id} criado com {criados} jobs no órgão FAKE.")
    print("Ative o órgão FAKE no config.toml (ativo = true) e rode:  cnd rodar --ate-esvaziar")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cnd", description="RPA de certidões fiscais")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("importar", help="importa uma planilha e cria um lote")
    p.add_argument("planilha", type=Path)
    p.add_argument("--descricao", default="")
    p.add_argument("--abas", nargs="*", default=["RFB"])
    p.set_defaults(func=_importar)

    p = sub.add_parser("rodar", help="sobe o orquestrador (o robô)")
    p.add_argument("--ate-esvaziar", action="store_true",
                   help="encerra assim que a fila zerar, sem esperar a "
                        "recuperação das falhas (usado em teste). Sem o flag, "
                        "o robô já encerra sozinho quando não sobra nada — "
                        "nem fila, nem falha a recuperar")
    p.add_argument("--config", type=Path, default=None,
                   help="outro config.toml (padrão: o da raiz do projeto)")
    p.add_argument("--limite", type=int, default=None,
                   help="para depois de N jobs (modo piloto)")
    p.add_argument("--forcar", action="store_true",
                   help="sobe mesmo se outro orquestrador parecer vivo")
    p.add_argument("--reiniciar-ritmo", action="store_true",
                   help="esquece o ritmo e o disjuntor aprendidos "
                        "(usar ao trocar de adapter)")
    p.set_defaults(func=_rodar)

    p = sub.add_parser("zerar",
                       help="apaga TUDO desta máquina (planilhas, itens e PDFs)")
    p.add_argument("--sim", action="store_true",
                   help="confirma: sem isto o comando só explica o que faria")
    p.add_argument("--config", type=Path, default=None)
    p.set_defaults(func=_zerar)

    p = sub.add_parser("painel", help="sobe o painel web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--porta", type=int, default=8000)
    p.set_defaults(func=_painel)

    p = sub.add_parser("inicio-automatico",
                       help="faz o painel subir sozinho junto com o Windows")
    p.add_argument("--no-boot", action="store_true",
                   help="sobe no boot, sem esperar logon (pede administrador)")
    p.add_argument("--remover", action="store_true",
                   help="desfaz: o painel volta a depender de subida à mão")
    p.set_defaults(func=_inicio_automatico)

    p = sub.add_parser("relatorio", help="gera o Excel do mês")
    p.add_argument("--mes", default=None, help="ex.: 2026-08 (padrão: o mês corrente)")
    p.add_argument("--orgao", default=None, help="ex.: RFB_PJ (padrão: todos)")
    p.add_argument("--saida", type=Path, default=None)
    p.set_defaults(func=_relatorio)

    p = sub.add_parser("app", help="abre o aplicativo de mesa")
    p.set_defaults(func=_app)

    p = sub.add_parser("testar-alerta", help="envia um aviso de teste")
    p.add_argument("--config", type=Path, default=None)
    p.set_defaults(func=_testar_alerta)

    p = sub.add_parser("calibrar", help="ensina ao robô cego onde ficam os campos na tela")
    p.add_argument("--orgao", default="RFB_CEGO")
    p.add_argument("--saida", type=Path, default=None)
    p.add_argument("--conferir", action="store_true",
                   help="não recalibra: desenha os pontos salvos sobre uma foto da tela")
    p.set_defaults(func=_calibrar)

    p = sub.add_parser("simular", help="cria um lote falso para testar offline")
    p.add_argument("quantidade", type=int, nargs="?", default=100)
    p.set_defaults(func=_simular)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
