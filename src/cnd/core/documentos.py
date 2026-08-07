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

def validar_cnpj(valor) -> str:
    """Devolve o CNPJ limpo se for válido. Se não for, acusa o erro."""
    doc = _limpar(valor)

    # O Excel guarda CNPJ como número e come os zeros da esquerda.
    if doc.isdigit() and len(doc) < 14:
        doc = doc.zfill(14)

    if len(doc) != 14:
        raise DocumentoInvalido(f"CNPJ deve ter 14 caracteres, tem {len(doc)}: {doc!r}")
    if not RE_CNPJ.match(doc):
        raise DocumentoInvalido(f"CNPJ com caracteres inválidos: {doc!r}")
    if len(set(doc)) == 1:
        raise DocumentoInvalido(f"CNPJ com todos os caracteres iguais: {doc!r}")

    base = [ord(c) - 48 for c in doc[:12]]
    dv1 = _dv_cnpj(base)
    dv2 = _dv_cnpj([*base, dv1])
    if doc[12:] != f"{dv1}{dv2}":
        raise DocumentoInvalido(f"CNPJ com dígito verificador inválido: {doc!r}")
    return doc

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
