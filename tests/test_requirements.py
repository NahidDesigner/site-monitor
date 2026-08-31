"""The requirements files have to survive pip's own parser.

A comment reading `content-encoding: br` on line 2 of requirements.txt once
broke the image build outright: pip sniffs the first two lines for a PEP 263
marker and tried to decode the whole file as encoding "br".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Exactly the check pip performs in pip._internal.req.req_file._decode_req_file.
PIP_ENCODING_RE = re.compile(rb"coding[:=]\s*([-\w.]+)")

REQUIREMENTS = sorted(Path(__file__).resolve().parent.parent.glob("requirements*.txt"))


def test_there_are_requirements_files_to_check():
    assert REQUIREMENTS, "expected requirements.txt to exist"


@pytest.mark.parametrize("path", REQUIREMENTS, ids=lambda p: p.name)
def test_pip_does_not_mistake_a_comment_for_an_encoding_declaration(path: Path):
    sniffed = [
        (number, PIP_ENCODING_RE.search(line).group(1).decode("ascii"))
        for number, line in enumerate(path.read_bytes().split(b"\n")[:2], start=1)
        if line[0:1] == b"#" and PIP_ENCODING_RE.search(line)
    ]

    assert not sniffed, (
        f"{path.name} line {sniffed[0][0]} looks like a PEP 263 marker to pip, "
        f"which would decode the file as {sniffed[0][1]!r} and fail the build"
    )


@pytest.mark.parametrize("path", REQUIREMENTS, ids=lambda p: p.name)
def test_every_requirement_line_is_a_parseable_specifier(path: Path):
    from packaging.requirements import Requirement

    for line in path.read_text().splitlines():
        stripped = line.strip()
        # `-r other.txt`, `--index-url ...` etc. are pip options, not specifiers.
        if not stripped or stripped.startswith(("#", "-")):
            continue
        Requirement(stripped)  # raises InvalidRequirement if malformed


def test_brotli_is_pinned_so_cloudflare_pages_can_be_decoded():
    text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()

    assert re.search(r"^brotli[>=]", text, re.MULTILINE | re.IGNORECASE)
