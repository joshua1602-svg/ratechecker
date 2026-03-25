"""Helpers for robust subject-property exclusion from comparable pools."""
from __future__ import annotations

import re
from typing import Any

_POSTCODE_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^A-Z0-9\s]")
_MULTI_WS_RE = re.compile(r"\s+")

# Conservative normalisations for common UK address-token variants.
_TOKEN_NORMALISATION = {
    "ST": "STREET",
    "ST.": "STREET",
    "RD": "ROAD",
    "RD.": "ROAD",
    "AVE": "AVENUE",
    "AVE.": "AVENUE",
    "LN": "LANE",
    "LN.": "LANE",
    "PL": "PLACE",
    "PL.": "PLACE",
    "SQ": "SQUARE",
    "SQ.": "SQUARE",
    "CT": "COURT",
    "CT.": "COURT",
    "TER": "TERRACE",
    "TER.": "TERRACE",
    "DR": "DRIVE",
    "DR.": "DRIVE",
    "CL": "CLOSE",
    "CL.": "CLOSE",
    "GF": "GROUND FLOOR",
    "LG": "LOWER GROUND",
}


def normalise_postcode(postcode: str | None) -> str:
    if not postcode:
        return ""
    return _POSTCODE_WS_RE.sub("", postcode.strip().upper())


def normalise_address(address: str | None) -> str:
    if not address:
        return ""
    text = address.strip().upper()
    text = text.replace("'", "")
    text = _PUNCT_RE.sub(" ", text)
    text = _MULTI_WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    tokens = [_TOKEN_NORMALISATION.get(tok, tok) for tok in text.split(" ")]
    return " ".join(tokens)


def _normalised_id(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def exclude_subject_rows(
    rows: list[dict],
    *,
    subject_id: str | None,
    subject_address: str,
    subject_postcode: str,
    id_key: str = "uarn",
    address_key: str = "address",
    postcode_key: str = "postcode",
) -> tuple[list[dict], dict[str, int]]:
    """Exclude the subject property from comparable rows.

    Priority order:
      1) Hard identifier exclusion when a subject_id is available.
      2) Fallback normalized address + postcode exclusion.
    """
    norm_subject_addr = normalise_address(subject_address)
    norm_subject_pc = normalise_postcode(subject_postcode)
    norm_subject_id = _normalised_id(subject_id)

    filtered: list[dict] = []
    removed_by_id = 0
    removed_by_address = 0

    for row in rows:
        row_id = _normalised_id(row.get(id_key))
        if norm_subject_id and row_id and row_id == norm_subject_id:
            removed_by_id += 1
            continue

        row_pc = normalise_postcode(row.get(postcode_key))
        row_addr = normalise_address(row.get(address_key))
        if (
            norm_subject_addr
            and norm_subject_pc
            and row_pc == norm_subject_pc
            and row_addr == norm_subject_addr
        ):
            removed_by_address += 1
            continue

        filtered.append(row)

    return filtered, {
        "removed_by_id": removed_by_id,
        "removed_by_address": removed_by_address,
    }
