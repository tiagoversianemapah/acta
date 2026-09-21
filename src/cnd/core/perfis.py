"""Fatos sobre um órgão que mudam o comportamento do núcleo.

Existe para não haver três listas dizendo a mesma coisa em três arquivos.
Quando entrar o segundo órgão com captcha próprio — SEFAZ-GO e SEFAZ-ES
estão no caminho —, quem esquecer de atualizar uma das listas ganha um robô
que pune o ritmo por um erro que é nosso, e ninguém descobre isso lendo o
código: descobre olhando um lote lento.
"""
from __future__ import annotations

# Órgãos cujo captcha é lido pelo NOSSO OCR, e não uma defesa do portal
# contra robô. O desfecho CAPTCHA nesses órgãos quer dizer "a leitura da
# imagem falhou", e não "o portal nos barrou" — daí três consequências:
#
#   1. não pune o ritmo            (orquestrador.worker._ajustar_ritmo)
#   2. retenta na hora, sem espera (orquestrador.worker._espera_retry)
#   3. não alimenta o disjuntor    (core.breaker.ativo_para)
#
# Punir o ritmo por erro de OCR foi o que deixou o MA arrastado em
# 15/09/2026: o robô ia ficando mais lento a cada imagem mal lida, como se
# o portal estivesse reclamando — e o portal não tinha reclamado de nada.
CAPTCHA_LIDO_POR_NOS = frozenset({"SEFAZ_MA"})

# Órgãos em que o captcha se resolve REABRINDO a sessão, e não esperando.
# No SEFAZ-ES o Turnstile passa invisível numa janela nova: quem o vê é uma
# sessão marcada, e o conserto é fechar o Edge e abrir outro — foi o que a
# operação observou na prática (18/09/2026). Antes disso o robô insistia no
# botão e depois pausava o órgão por 30 min, esperando 1h, 4h e 12h entre as
# tentativas: horas paradas por algo que uma janela nova resolve.
#
# A consequência é a mesma do captcha lido por nós: não punir o ritmo, não
# esperar e não alimentar o disjuntor. O que muda é o motivo.
CAPTCHA_RESOLVIDO_REABRINDO = frozenset({"SEFAZ_ES"})


def captcha_lido_por_nos(orgao: str) -> bool:
    """O captcha deste órgão é leitura nossa, não defesa do portal?"""
    return orgao in CAPTCHA_LIDO_POR_NOS


def captcha_sem_castigo(orgao: str) -> bool:
    """Captcha aqui não pede recuo: ou é erro nosso, ou some com sessão nova.

    Nos dois casos, punir o ritmo e esperar horas só para o lote — e o
    portal não pediu nada disso.
    """
    return orgao in CAPTCHA_LIDO_POR_NOS or orgao in CAPTCHA_RESOLVIDO_REABRINDO
