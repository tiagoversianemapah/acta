"""Comandos de MANUTENÇÃO: conferir o aviso, e zerar a máquina.

São os dois comandos que ninguém roda no dia a dia — um confere que o aviso
sai antes de ele fazer falta, o outro apaga tudo e não tem desfazer.
"""
from __future__ import annotations

from pathlib import Path


def zerar_maquina(args) -> int:
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


def testar_alerta(args) -> int:
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


def registrar(sub) -> None:
    p = sub.add_parser("zerar",
                       help="apaga TUDO desta máquina (planilhas, itens e PDFs)")
    p.add_argument("--sim", action="store_true",
                   help="confirma: sem isto o comando só explica o que faria")
    p.add_argument("--config", type=Path, default=None)
    p.set_defaults(func=zerar_maquina)

    p = sub.add_parser("testar-alerta", help="envia um aviso de teste")
    p.add_argument("--config", type=Path, default=None)
    p.set_defaults(func=testar_alerta)

