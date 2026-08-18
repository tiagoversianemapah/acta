"""Validação de CNPJ e CPF."""
from __future__ import annotations

import re

RE_NAO_ALFANUM = re.compile(r"[^0-9A-Za-z]")
RE_CNPJ = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")
RE_CPF = re.compile(r"^[0-9]{11}$")


class DocumentoInvalido(ValueError):
    """Documento reprovado. A mensagem explica o motivo."""


def _limpar(valor) -> str:
    """Remove pontos, barras, traços e espaços."""
    return RE_NAO_ALFANUM.sub("", str(valor or "")).upper()


def limpar(valor) -> str:
    """Normaliza documento ou busca de documento para comparação."""
    return _limpar(valor)


def _dv_cnpj(valores: list[int]) -> int:
    """Calcula um dígito verificador do CNPJ."""
    soma = 0
    peso = 2
    for valor in reversed(valores):
        soma += valor * peso
        peso = peso + 1 if peso < 9 else 2
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def _dv_cpf(digitos: list[int]) -> int:
    """Calcula um dígito verificador do CPF."""
    pesos = range(len(digitos) + 1, 1, -1)
    # strict=True: os dois têm o mesmo tamanho por construção, e se um dia
    # deixarem de ter, o dígito sairia calmamente errado — o pior tipo de
    # defeito num validador de documento.
    soma = sum(d * p for d, p in zip(digitos, pesos, strict=True))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def _parece_cpf(digitos: str) -> bool:
    """11 dígitos que passam no DV de CPF."""
    if not RE_CPF.match(digitos):
        return False
    try:
        validar_cpf(digitos)
    except DocumentoInvalido:
        return False
    return True


def validar_cnpj(valor) -> str:
    """Devolve o CNPJ limpo se for válido. Se não for, acusa o erro."""
    doc = _limpar(valor)
    original = doc

    # O Excel guarda CNPJ como número e come os zeros da esquerda.
    if doc.isdigit() and len(doc) < 14:
        doc = doc.zfill(14)

    def recusar(motivo: str) -> DocumentoInvalido:
        """Diz "isto é um CPF" quando for, em vez de acusar CNPJ torto.

        Um CPF na aba de CNPJ não é erro de digitação, é linha na aba
        errada — e a mensagem precisa dizer isso, senão a pessoa fica
        conferindo um número que está certo. Pior: o preenchimento com
        zeros acima transforma 11 dígitos em 14, e a queixa saía sobre
        um número que ninguém digitou.
        """
        if _parece_cpf(original):
            return DocumentoInvalido(
                f"{formatar(original)} é um CPF, e esta automação consulta "
                f"CNPJ. Mova a linha para a aba CPF."
            )
        return DocumentoInvalido(motivo)

    if len(doc) != 14:
        raise recusar(
            f"CNPJ deve ter 14 caracteres, tem {len(original)}: {original!r}")
    if not RE_CNPJ.match(doc):
        raise recusar(f"CNPJ com caracteres inválidos: {doc!r}")
    if len(set(doc)) == 1:
        raise recusar(f"CNPJ com todos os caracteres iguais: {doc!r}")

    base = [ord(c) - 48 for c in doc[:12]]
    dv1 = _dv_cnpj(base)
    dv2 = _dv_cnpj([*base, dv1])
    if doc[12:] != f"{dv1}{dv2}":
        raise recusar(f"CNPJ com dígito verificador inválido: {doc!r}")
    return doc


def cnpj_da_matriz(valor) -> str:
    """CNPJ da matriz correspondente ao CNPJ informado.

    A Receita Federal emite a CND de pessoa juridica pela matriz. Quando a
    carteira vem com filial, o portal devolve um aviso pedindo o CNPJ 0001.
    """
    doc = validar_cnpj(valor)
    if doc[8:12] == "0001":
        return doc

    base = f"{doc[:8]}0001"
    valores = [ord(c) - 48 for c in base]
    dv1 = _dv_cnpj(valores)
    dv2 = _dv_cnpj([*valores, dv1])
    return f"{base}{dv1}{dv2}"


def validar_cpf(valor) -> str:
    """Devolve o CPF limpo se for válido. Se não for, acusa o erro."""
    doc = _limpar(valor)

    if doc.isdigit() and len(doc) < 11:
        doc = doc.zfill(11)

    if not RE_CPF.match(doc):
        raise DocumentoInvalido(f"CPF deve ter 11 dígitos: {doc!r}")
    if len(set(doc)) == 1:
        raise DocumentoInvalido(f"CPF com todos os dígitos iguais: {doc!r}")

    base = [int(c) for c in doc[:9]]
    dv1 = _dv_cpf(base)
    dv2 = _dv_cpf([*base, dv1])
    if doc[9:] != f"{dv1}{dv2}":
        raise DocumentoInvalido(f"CPF com dígito verificador inválido: {doc!r}")
    return doc


def validar(valor, tipo: str) -> str:
    """Valida conforme o tipo: 'CNPJ' ou 'CPF'."""
    if tipo == "CNPJ":
        return validar_cnpj(valor)
    if tipo == "CPF":
        return validar_cpf(valor)
    raise ValueError(f"Tipo de documento desconhecido: {tipo!r}")


def formatar(documento: str) -> str:
    """Coloca a máscara de volta, só para exibir na tela."""
    d = documento
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d
