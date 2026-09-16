"""Comandos do ROBÔ: pôr para trabalhar e ensinar onde clicar.

`rodar` é o orquestrador; `calibrar` só existe por causa do robô cego, que
trabalha por coordenada de tela e precisa das medidas daquela máquina.
"""
from __future__ import annotations

from pathlib import Path


def rodar_robo(args) -> int:
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


def calibrar_tela(args) -> int:
    from cnd.adapters.calibragem import calibrar, conferir
    from cnd.infra.db import RAIZ_PROJETO

    destino = args.saida or (RAIZ_PROJETO / "data" / "calibragem" /
                             f"{args.orgao.lower()}.json")
    if args.conferir:
        conferir(destino, destino.with_name(f"{args.orgao.lower()}-conferencia.png"),
                 orgao=args.orgao)
    else:
        calibrar(destino, orgao=args.orgao)
    return 0


def registrar(sub) -> None:
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
    p.set_defaults(func=rodar_robo)

    p = sub.add_parser("calibrar",
                       help="ensina ao robô cego onde ficam os campos na tela")
    p.add_argument("--orgao", default="RFB_CEGO")
    p.add_argument("--saida", type=Path, default=None)
    p.add_argument("--conferir", action="store_true",
                   help="não recalibra: desenha os pontos salvos sobre uma foto da tela")
    p.set_defaults(func=calibrar_tela)

