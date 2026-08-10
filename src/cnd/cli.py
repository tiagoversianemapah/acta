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
    uvicorn.run("cnd.web.app:app", host=args.host, port=args.porta, log_level="warning")
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
                   help="encerra quando a fila zerar (usado em teste)")
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

    p = sub.add_parser("painel", help="sobe o painel web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--porta", type=int, default=8000)
    p.set_defaults(func=_painel)

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
