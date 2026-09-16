"""Comandos que abrem TELA: o painel web e o aplicativo de mesa.

Junto vem `inicio-automatico`, que é sobre a mesma tela — ela subir com o
Windows em vez de depender de alguém lembrar.
"""
from __future__ import annotations


def subir_painel(args) -> int:
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


def abrir_aplicativo(args) -> int:
    from cnd.desktop.app import main as abrir

    return abrir()


def configurar_inicio_automatico(args) -> int:
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


def registrar(sub) -> None:
    p = sub.add_parser("painel", help="sobe o painel web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--porta", type=int, default=8000)
    p.set_defaults(func=subir_painel)

    p = sub.add_parser("app", help="abre o aplicativo de mesa")
    p.set_defaults(func=abrir_aplicativo)

    p = sub.add_parser("inicio-automatico",
                       help="faz o painel subir sozinho junto com o Windows")
    p.add_argument("--no-boot", action="store_true",
                   help="sobe no boot, sem esperar logon (pede administrador)")
    p.add_argument("--remover", action="store_true",
                   help="desfaz: o painel volta a depender de subida à mão")
    p.set_defaults(func=configurar_inicio_automatico)

