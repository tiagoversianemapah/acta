"""Comandos que mexem em LOTE: entrar trabalho, e tirar resultado dele.

`importar` cria o lote a partir da planilha, `simular` cria um lote falso
para exercitar o sistema sem portal, e `relatorio` tira o Excel do mês.
"""
from __future__ import annotations

from pathlib import Path


def importar_planilha(args) -> int:
    from cnd.infra.config import carregar
    from cnd.infra.db import conectar, criar_schema, garantir
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

    cfg = carregar(args.config)
    garantir(cfg.banco)
    conn = conectar(cfg.banco)
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


def gerar_relatorio(args) -> int:
    from cnd.infra.db import conectar_leitura
    from cnd.web.relatorio import gerar

    conn = conectar_leitura()
    from cnd.web.relatorio import Recorte, mes_corrente

    destino = gerar(conn, Recorte(args.mes or mes_corrente(), args.orgao),
                    args.saida)
    conn.close()
    print(f"Relatório gerado: {destino}")
    return 0


def simular_lote(args) -> int:
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


def registrar(sub) -> None:
    p = sub.add_parser("importar", help="importa uma planilha e cria um lote")
    p.add_argument("planilha", type=Path)
    p.add_argument("--descricao", default="")
    p.add_argument("--abas", nargs="*", default=["RFB"])
    p.add_argument("--config", type=Path, default=None)
    p.set_defaults(func=importar_planilha)

    p = sub.add_parser("relatorio", help="gera o Excel do mês")
    p.add_argument("--mes", default=None, help="ex.: 2026-08 (padrão: o mês corrente)")
    p.add_argument("--orgao", default=None, help="ex.: RFB_PJ (padrão: todos)")
    p.add_argument("--saida", type=Path, default=None)
    p.set_defaults(func=gerar_relatorio)

    p = sub.add_parser("simular", help="cria um lote falso para testar offline")
    p.add_argument("quantidade", type=int, nargs="?", default=100)
    p.set_defaults(func=simular_lote)

