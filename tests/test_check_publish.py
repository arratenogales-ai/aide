"""scripts/check_publish.py: what must not appear in the repository. The suspicious values
are built on the fly (in parts) so that this file does not contain them and the
repository's own check stays clean."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_publish", ROOT / "scripts/check_publish.py")
assert _spec is not None and _spec.loader is not None
cp = importlib.util.module_from_spec(_spec)
sys.modules["check_publish"] = cp
_spec.loader.exec_module(cp)


def _png(chunks: list[tuple[bytes, bytes]]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = [chunk(b"IHDR", header), *(chunk(t, d) for t, d in chunks)]
    body += [chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")), chunk(b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(body)


def _reasons(root: Path) -> list[str]:
    return [h.text() for h in cp.review(root)]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text(
        ".env\n.env.*\n!.env.example\ndata/\n__pycache__/\n*.py[cod]\n/solo_raiz.txt\n"
        "docs/**/borrador.md\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("Built with the Claude Agent SDK.\n", encoding="utf-8")
    return tmp_path


# --- .gitignore ------------------------------------------------------------------------------


def test_only_what_gitignore_does_not_ignore(repo):
    for relative in (
        ".env",
        ".env.local",
        "data/agent.db",
        "src/__pycache__/x.pyc",
        "src/x.pyc",
        "solo_raiz.txt",
        "docs/a/b/borrador.md",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    for relative in (".env.example", "src/solo_raiz.txt", "docs/a/final.md"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    publishable = sorted(p.relative_to(repo).as_posix() for p in cp.publishable_files(repo))
    assert publishable == [
        ".env.example",
        ".gitignore",
        "README.md",
        "docs/a/final.md",
        "src/solo_raiz.txt",
    ]
    assert _reasons(repo) == []


def test_repository_is_clean():
    assert _reasons(ROOT) == []


# --- secrets ----------------------------------------------------------------------------------

SECRETS = {
    "Telegram bot token": "1234567" + "89:" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345678",
    "Google API key": "AI" + "za" + "SyA1234567890abcdefghijklmnopqrstuv",
    "Google OAuth client secret": "GOC" + "SPX-" + "abcdefghijklmnopqrstuvwx1234",
    "Google refresh token": "1/" + "/0" + "gAbCdEfGhIjKlMnOpQrStUvWxYz",
    "Anthropic key or token": "sk-" + "ant-" + "api03-" + "Zz9" * 10,
    "private key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "AWS access key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "GitHub token": "gh" + "p_" + "a1B2" * 9,
}


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_finds_tokens_and_keys(repo, name):
    (repo / "config.py").write_text(f'VALOR = "{SECRETS[name]}"\n', encoding="utf-8")
    assert any(name in m and "config.py:1" in m for m in _reasons(repo))


def test_exceptions_apply_only_to_the_exact_string(repo):
    token = SECRETS["Anthropic key or token"]
    (repo / "test_x.py").write_text(f'A = "{token}"\nB = "{token}X"\n', encoding="utf-8")
    (repo / cp.EXCEPTIONS_FILE).write_text(f"# fake\n{token}\n", encoding="utf-8")
    assert _reasons(repo) == ["test_x.py:2: looks like a Anthropic key or token"]


# --- mails and paths --------------------------------------------------------------------------


def test_emails_only_from_example_domains(repo):
    good = [
        "a" + "@example.com",
        "b" + "@mail.example.org",
        "c" + "@shop.test",
        "d" + "@x.example",
    ]
    bad = "alex.doe" + "@" + "realmail.com"
    (repo / "notas.md").write_text(" ".join(good) + "\n" + bad + "\n", encoding="utf-8")
    assert _reasons(repo) == [f"notas.md:2: email address {bad}"]


def test_paths_of_a_personal_folder(repo):
    path = "/Use" + "rs/alex/Documents/x"
    (repo / "a.sh").write_text(f"cd {path}\ncd /Users/Shared/x\ncd /usr/local\n", encoding="utf-8")
    assert _reasons(repo) == ["a.sh:1: personal absolute path " + path.rsplit("/", 2)[0]]


# --- files that must not go out ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "credentials.json",
        "token.json",
        "copia.db",
        "agente.sqlite3",
        "contrato.pdf",
        "carta.docx",
        "cuentas.xlsx",
        "nota.m4a",
        "voz.ogg",
        "clave.pem",
        "sesion.jsonl",
        "NOTAS_SESION.md",
        "DECISIONES.md",
    ],
)
def test_files_that_are_not_published(repo, name):
    (repo / "sub").mkdir()
    (repo / "sub" / name).write_bytes(b"x")
    assert any(m.startswith(f"sub/{name}") for m in _reasons(repo))


def test_files_over_2_mb(repo):
    with open(repo / "grande.bin", "wb") as f:
        f.truncate(cp.BYTES_LIMIT + 1)
    assert any("larger than 2 MB" in m for m in _reasons(repo))


def test_images_with_metadata(repo):
    (repo / "limpia.png").write_bytes(_png([]))
    assert _reasons(repo) == []
    (repo / "texto.png").write_bytes(_png([(b"tEXt", b"Author\x00someone")]))
    (repo / "hora.png").write_bytes(_png([(b"tIME", b"\x07\xea\x0a\x1c\x0c\x00\x00")]))
    exif = b"Exif\x00\x00II*\x00"
    jpeg = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + b"\xff\xd9"
    (repo / "foto.jpg").write_bytes(jpeg)
    assert sorted(_reasons(repo)) == [
        "foto.jpg: image metadata (EXIF)",
        "hora.png: image metadata (tIME)",
        "texto.png: image metadata (tEXt)",
    ]


# --- authorship -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Co-" + "Authored-By: Someone <x@example.com>",
        "Generated " + "with [Claude Code](https://example.com)",
        "This README was written " + "by Claude.",
        "AI-" + "generated content",
        "Contact: noreply" + "@anthropic.com",
        "Done \U0001f916",
        "Este código está generado " + "por IA.",
        "Texto escrito " + "con Claude.",
    ],
)
def test_authorship_mentions(repo, text):
    (repo / "notas.md").write_text(text + "\n", encoding="utf-8")
    assert _reasons(repo), text


def test_technical_uses_of_claude_do_not_count(repo):
    (repo / "README.md").write_text(
        "Aide is built with the Claude Agent SDK and runs on Claude models.\n"
        "It uses the Claude Code CLI bundled with the SDK and reads agent/CLAUDE.md.\n"
        "Built with Claude Agent SDK sessions. Sign in with Claude Code's /login.\n"
        "Hecho con Claude Agent SDK.\n",
        encoding="utf-8",
    )
    assert _reasons(repo) == []


def test_output_and_exit_code(repo, capsys):
    assert cp.main([str(repo)]) == 0
    assert "Clean" in capsys.readouterr().out
    (repo / ".env").write_text("x", encoding="utf-8")  # ignored: does not count
    (repo / "credentials.json").write_text("{}", encoding="utf-8")
    assert cp.main([str(repo)]) == 1
    assert "credentials.json: Google OAuth client file" in capsys.readouterr().out
