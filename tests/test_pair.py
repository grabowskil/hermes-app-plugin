import io
from urllib.parse import parse_qs, urlparse

from hermes_app import pair


def test_build_link_roundtrip():
    link = pair.build_link("http://host.example:8642", "se&cret/+=", "My Mac")
    parts = urlparse(link)
    assert (parts.scheme, parts.netloc) == ("hermes", "pair")
    q = parse_qs(parts.query)
    assert q == {"u": ["http://host.example:8642"], "k": ["se&cret/+="], "n": ["My Mac"]}


def test_default_url(monkeypatch):
    monkeypatch.setenv("API_SERVER_HOST", "10.1.2.3")
    monkeypatch.setenv("API_SERVER_PORT", "9000")
    assert pair.default_url() == "http://10.1.2.3:9000"
    monkeypatch.delenv("API_SERVER_PORT")
    assert pair.default_url() == "http://10.1.2.3:8642"


def test_run_prints_qr_and_link(monkeypatch):
    monkeypatch.setattr(pair, "_api_key", lambda: "k" * 32)
    out = io.StringIO()
    assert pair.run("http://h:8642/", "Box", out=out) == 0
    last = out.getvalue().strip().splitlines()[-1]
    assert last == pair.build_link("http://h:8642", "k" * 32, "Box")
    assert len(out.getvalue().splitlines()) > 10  # the QR drawing


def test_run_without_key_fails(monkeypatch, capsys):
    monkeypatch.setattr(pair, "_api_key", lambda: "")
    assert pair.run("http://h:8642", "Box", out=io.StringIO()) == 1
    assert "API_SERVER_KEY" in capsys.readouterr().err
