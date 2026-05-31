"""reverify_cli 서브커맨드 테스트."""
import json

from reverify_cli import main as cli_main


def _setup(tmp_path, monkeypatch):
    code = tmp_path / "code"
    (code / "src").mkdir(parents=True)
    (code / "src" / "memory_indexer.py").write_text(
        'EMBED_URL = "http://localhost:8081/embed"\n# Arctic\n', encoding="utf-8"
    )
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "stale.md").write_text("---\nname: s\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    (mem / "fresh.md").write_text("---\nname: f\n---\n\narctic-ko\n", encoding="utf-8")
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(code))
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    return mem


def test_cli_scan_flags(tmp_path, monkeypatch, capsys):
    mem = _setup(tmp_path, monkeypatch)
    rc = cli_main(["scan", str(mem), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["flagged"] == 1
    assert "reverify_status: stale" in (mem / "stale.md").read_text(encoding="utf-8")


def test_cli_list_shows_stale(tmp_path, monkeypatch, capsys):
    mem = _setup(tmp_path, monkeypatch)
    cli_main(["scan", str(mem)])
    capsys.readouterr()
    rc = cli_main(["list", str(mem)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale.md" in out
    assert "fresh.md" not in out


def test_cli_verify_registry_ok_on_real_repo(monkeypatch, capsys):
    monkeypatch.delenv("MV3_REVERIFY_ROOT", raising=False)  # 실제 repo root 사용
    rc = cli_main(["verify-registry"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out   # capsys 는 1회만 호출 (버퍼 소비)


def test_cli_verify_registry_detects_stale_registry(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad"
    (bad / "src").mkdir(parents=True)
    (bad / "src" / "memory_indexer.py").write_text("nothing\n", encoding="utf-8")
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(bad))
    rc = cli_main(["verify-registry"])
    assert rc == 1   # verifier fail → 비정상 exit
    assert "embedding_model" in capsys.readouterr().out
