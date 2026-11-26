"""Nothing that ships may identify the machine a measurement was taken on.

Docs here are written as war stories, and a war story picks up the bench's address, an
account name or a token on the way. Those are not style problems: an address says which
network a machine sits on, and a credential in a doc is a credential. The rules are
shapes rather than values on purpose -- the values that belong to a particular bench are
nobody's business, and a check that named them would leak them by itself.

This file is the one thing it does not scan: a rule that spells out the shape of a
password assignment contains a password assignment.
"""
import ipaddress
import re

import pytest

from repo import ROOT

SCAN_SUFFIXES = {".md", ".sh", ".py", ".yaml", ".yml", ".c", ".h", ".patch", ".conf",
                 ".txt", ".json", "Makefile"}
SELF = "test_privacy.py"

# Loopback is what the README tells people to browse to; 0.0.0.0 is what the installer
# greps out of `docker port`. Anything else in the private ranges has no business here.
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0", "127.0.1.1"}

# Dotted quads that are not addresses, each with the reason it is not one. A public
# address would be as much of a leak as a private one, so every quad that is not loopback
# has to be claimed here by somebody who read the line.
KNOWN_NOT_ADDRESSES = {
    "7.5.3.14": "section of the PCIe Base 4.0 spec, cited in tools/hammer.sh",
}

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ADDRESS_RULE = "private or link-local IPv4 address"
EXPLAINED = ALLOWED_IPS | set(KNOWN_NOT_ADDRESSES)

PATTERNS = [
    (ADDRESS_RULE, IPV4),
    ("user@host, which is an account name on someone's machine",
     re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")),
    ("password, token or key assigned to something",
     re.compile(r"\b(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)"
                r"\s*[:=]\s*\S", re.I)),
    ("private key material", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("password handed to ssh on a command line", re.compile(r"\bsshpass\b|\bSSHPASS=")),
    ("Windows or WSL path of one person's home directory",
     re.compile(r"[A-Za-z]:\\+[Uu]sers|[Cc]:/Users|/mnt/c/[Uu]sers|/home/[a-z0-9_]+/")),
    ("a named checkout outside the repository", re.compile(r"~/[A-Za-z0-9._-]+-build\b")),
]


def shipped_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.name == SELF:
            continue
        if path.suffix not in SCAN_SUFFIXES and path.name not in SCAN_SUFFIXES:
            continue
        yield path


@pytest.fixture(scope="module")
def corpus():
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in shipped_files()}


def hits_for(corpus):
    """(posix path, line number, rule, matched text, line) for every shape above."""
    out = []
    for path, text in corpus.items():
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    out.append((rel, number, rule, match.group(0), line.strip()))
    return out


def address_hits(hits):
    """The hits that are addresses in the sense that matters here.

    Only ranges that identify a network, and guarded against `999.999.999.999`, which the
    pattern accepts and the parser does not: a check that raises on a weird line is a
    check that gets deleted.
    """
    out = []
    for hit in hits:
        if hit[2] != ADDRESS_RULE or hit[3] in EXPLAINED:
            continue
        try:
            addr = ipaddress.ip_address(hit[3])
        except ValueError:
            continue
        if addr.is_private or addr.is_link_local:
            out.append(hit)
    return out


def format_hit(hit):
    rel, number, rule, token, line = hit
    return f"  {rel}:{number}  {rule}  ->  {token!r}   in: {line[:88]}"


def test_no_machine_on_a_private_network_is_named(corpus):
    """The addresses that say which network a bench sits on.

    A public address is checked by name rather than by range, in the next test.
    """
    bad = address_hits(hits_for(corpus))
    assert bad == [], "\n".join(format_hit(h) for h in bad)


def test_every_dotted_quad_is_either_loopback_or_explained(corpus):
    """The strict half: an unexplained public address is as much a leak as a private one.

    Because every quad has to be named, this also asks that the names be used -- an excuse
    left behind by a rewritten script has to go, or the list becomes the place mistakes
    are hidden instead of the place they are explained.
    """
    seen = set()
    for text in corpus.values():
        seen |= set(IPV4.findall(text))
    unexpected = seen - EXPLAINED
    assert unexpected == set(), f"unexplained dotted quads: {sorted(unexpected)}"

    stale = set(KNOWN_NOT_ADDRESSES) - seen
    assert stale == set(), f"no longer in the tree, delete the excuse: {sorted(stale)}"


def test_no_credential_shapes_ship(corpus):
    bad = [h for h in hits_for(corpus) if h[2] != ADDRESS_RULE]
    assert bad == [], "\n".join(format_hit(h) for h in bad)


def test_no_document_reaches_for_someone_elses_shell(corpus):
    """Docs are the usual way this leaks, so ask the question of them on its own.

    A doc may say `the bench` or `another machine on the network`; it may not say how to
    log into one, because the how is the part that carries a name.
    """
    bad = []
    for path, text in corpus.items():
        if path.suffix != ".md":
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\b(?:ssh|scp|sftp)\b\s+\S*@", line) or re.search(r"\bsshpass\b", line):
                bad.append(f"  {path.relative_to(ROOT).as_posix()}:{number}  {line.strip()[:88]}")
    assert bad == [], "docs reach for a shell on someone else's machine:\n" + "\n".join(bad)


def test_the_guard_would_actually_have_caught_it():
    """The rules are worth nothing if they only pass, so feed each one its own example.

    Written against the corpus helpers rather than against a fixture file, because the
    thing under test is the matcher, and a planted file in the tree would be caught by
    the tests above and read as a real leak.

    Each plant is assembled from two fragments, and that is not decoration: this file
    already has to contain the shape of a password assignment in order to check for one,
    so a plain-text second copy would read as a real credential to a person grepping the
    tree, and as a candidate to a secret scanner watching pushes.
    """
    host_a = "192.0.2." + "13"          # RFC 5737 documentation range
    host_b = "198.51.100." + "7"
    account = "operator" + "@" + "bench.example"
    secret = "pass" + "word = placeholder-not-real"
    key = "-----BEGIN " + "RSA PRIVATE KEY-----"
    ssh = "ssh" + "pass -e ssh analyst" + "@" + "host.example echo ok"
    home = "C:" + "\\" + "Users\\someone\\Desktop\\x.sh"
    checkout = "~/" + "bench-build"

    planted = {
        ROOT / "docs" / "planted.md": (
            f"取证是从 {host_a} 这台机器上做的，另一台是 {host_b}。\n"
            f"登录用 {account}，密码写死成 {secret}。\n"
            f"{key}\n"
            f"{ssh}\n"
            f"脚本在 {home}，构建目录是 {checkout}。\n"
        ),
    }
    caught = {h[2] for h in hits_for(planted)}
    for rule, _ in PATTERNS:
        assert rule in caught, f"the rule for {rule!r} matches nothing, even a plant"

    # What these have to read as: somebody's machine, not an example a test may skip.
    bad = [h[3] for h in address_hits(hits_for(planted))]
    assert sorted(bad) == [host_a, host_b], bad
