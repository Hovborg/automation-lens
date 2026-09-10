"""Public CLI contracts, using only synthetic files and fake API clients."""
import json
import sys
import urllib.request

import pytest

from test_kortslutning import ks, _FalskHa


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["automation-lens", *args])
    return ks.main()


def test_help_needs_no_cache(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert invoke(monkeypatch) == 0
    output = capsys.readouterr().out
    assert "Automation Lens" in output
    assert "--data-dir" in output


def test_english_findings_alias_reads_selected_folder(monkeypatch, tmp_path, capsys):
    ks.hent_via_api(_FalskHa(), tmp_path, skriv=lambda s: None)
    assert invoke(monkeypatch, "--data-dir", str(tmp_path), "--findings", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result and result[0]["kind"] == "conflicting-write"
    assert set(result[0]) == {"kind", "severity", "title", "trace", "automations"}


def test_json_severity_filter_is_applied(monkeypatch, tmp_path, capsys):
    ks.hent_via_api(_FalskHa(), tmp_path, skriv=lambda s: None)
    assert invoke(monkeypatch, "--data-dir", str(tmp_path), "--findings", "--severity", "critical", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_environment_data_folder_is_read_at_invocation(monkeypatch, tmp_path, capsys):
    ks.hent_via_api(_FalskHa(), tmp_path, skriv=lambda s: None)
    monkeypatch.setenv("KS_DATA_DIR", str(tmp_path))
    assert invoke(monkeypatch, "--fund", "--json") == 0
    assert json.loads(capsys.readouterr().out)


def test_missing_files_report_actionable_error(monkeypatch, tmp_path, capsys):
    assert invoke(monkeypatch, "--data-dir", str(tmp_path), "--findings") == 1
    assert "device_registry.json" in capsys.readouterr().err


def test_fetch_without_explicit_connection_does_not_connect(monkeypatch, capsys):
    for key in (*ks.HA_TOKEN_VARS, "KS_HA_URL", "HOMEASSISTANT_URL", "HASS_SERVER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ks, "HA_URL", "")
    monkeypatch.setattr(ks, "hent_via_api", lambda *a: pytest.fail("unexpected API call"))
    assert ks.hent() == 1
    assert "KS_HA_URL" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["file:///tmp/test", "http://user:password@ha.test", "https://ha.test/?token=secret"])
def test_invalid_connection_url_is_rejected_without_echoing_secrets(monkeypatch, capsys, url):
    monkeypatch.setattr(ks, "HA_URL", url)
    monkeypatch.setattr(ks, "ha_token", lambda: "test-token")
    monkeypatch.setattr(ks, "hent_via_api", lambda *a: pytest.fail("unexpected API call"))
    assert ks.hent() == 1
    assert "password" not in capsys.readouterr().err


def test_api_rejects_redirect_before_forwarding_token():
    request = urllib.request.Request("https://ha.test/api/states", headers={"Authorization": "Bearer fake"})
    with pytest.raises(ks.urllib.error.HTTPError):
        ks.NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.test/")


def test_bad_automation_yaml_is_an_error(monkeypatch, tmp_path, capsys):
    ks.hent_via_api(_FalskHa(), tmp_path, skriv=lambda s: None)
    (tmp_path / "automations.yaml").write_text("not: [valid", encoding="utf-8")
    assert invoke(monkeypatch, "--data-dir", str(tmp_path), "--findings") == 1
    assert "Could not read configuration" in capsys.readouterr().err


def test_demo_finds_synthetic_conflicts(monkeypatch, capsys):
    folder = ks.HERE / "examples" / "demo"
    assert invoke(monkeypatch, "--data-dir", str(folder), "--findings", "--json") == 0
    kinds = {f["kind"] for f in json.loads(capsys.readouterr().out)}
    assert {"feedback-loop", "conflicting-write", "missing-reference", "delay-trap"} <= kinds


def test_english_and_legacy_kind_filters_export_the_same_kind(monkeypatch, capsys):
    folder = ks.HERE / "examples" / "demo"
    for flag, value in (("--kind", "feedback-loop"), ("--art", "kortslutning")):
        assert invoke(monkeypatch, "--data-dir", str(folder), "--findings", flag, value, "--json") == 0
        findings = json.loads(capsys.readouterr().out)
        assert findings and {finding["kind"] for finding in findings} == {"feedback-loop"}


def test_demo_human_report_is_english(monkeypatch, capsys):
    folder = ks.HERE / "examples" / "demo"
    assert invoke(monkeypatch, "--data-dir", str(folder), "--findings") == 0
    output = capsys.readouterr().out
    assert "Automation Lens" in output
    assert "FEEDBACK LOOP" in output
    assert "Potential feedback loop between" in output
    assert "Tilbagekobling" not in output


def test_simulation_json_has_an_english_public_schema(monkeypatch, capsys):
    folder = ks.HERE / "examples" / "demo"
    assert invoke(monkeypatch, "--data-dir", str(folder), "light.demo_lamp", "on", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["start"] == {"entity": "light.demo_lamp", "state": "on", "time": "12:00"}
    assert set(result) == {"start", "outcome", "trace"}
    assert result["trace"] and all("type" in entry and "art" not in entry for entry in result["trace"])
