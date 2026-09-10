"""Automation Lens tests using pure helpers, small fixtures and a fake API.

Run: python -m pytest test_kortslutning.py -q
No test connects to Home Assistant.
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent


def _indlæs_modul():
    spec = importlib.util.spec_from_file_location("automation_lens_under_test", HERE / "automation_lens.py")
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul          # dataclasses slår modulet op under klassebygning
    spec.loader.exec_module(modul)
    return modul


ks = _indlæs_modul()


# ── registre og fixtures ───────────────────────────────────────────────────

ENHEDER = [
    {"id": "dev-stue", "name": "Stue Hue", "name_by_user": None, "area_id": "stue"},
    {"id": "dev-gang", "name": "Gang sensor", "name_by_user": "Gangsensoren", "area_id": "gang"},
]
ENTITETER = [
    {"entity_id": "light.stue_loft", "id": "reg-stue-loft", "device_id": "dev-stue", "area_id": None,
     "name": "Loftlampe", "original_name": None},
    {"entity_id": "switch.stue_stik", "id": "reg-stue-stik", "device_id": "dev-stue", "area_id": None,
     "name": None, "original_name": "Stikkontakt"},
    {"entity_id": "binary_sensor.gang_bevaegelse", "id": "reg-gang", "device_id": "dev-gang", "area_id": None,
     "name": None, "original_name": None},
    {"entity_id": "light.gang", "id": "reg-gang-lys", "device_id": None, "area_id": "gang",
     "name": "Ganglys", "original_name": None},
]


@pytest.fixture
def reg():
    return ks.Register(ENHEDER, ENTITETER)


def automation(alias, triggers, actions, *, mode="single", conditions=None, id=None):
    a = {"id": id or alias.lower().replace(" ", "_"), "alias": alias, "mode": mode,
         "triggers": triggers, "actions": actions}
    if conditions is not None:
        a["conditions"] = conditions
    return a


def state_trigger(entity, **ekstra):
    return {"trigger": "state", "entity_id": entity, **ekstra}


def turn(service, entity, **ekstra):
    return {"action": service, "target": {"entity_id": entity}, **ekstra}


def indlæs(reg, autos, tmp_path, kilde="automation"):
    sti = tmp_path / "automations.yaml"
    sti.write_text(yaml.safe_dump(autos, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return ks.indlæs(reg, sti, kilde)


# ── rene funktioner ────────────────────────────────────────────────────────

@pytest.mark.parametrize("værdi, forventet", [
    ("07:30", 450), ("07:30:00", 450), ("23:59", 1439), ("24:10", 10), ("x", None), (5, None), (None, None),
])
def test_tid_til_min(værdi, forventet):
    assert ks.tid_til_min(værdi) == forventet


@pytest.mark.parametrize("værdi, forventet", [
    (90, 90.0), ("00:02:30", 150.0), ("45", 45.0), ("abc", 0.0),
    ({"hours": 1, "minutes": 1, "seconds": 1, "milliseconds": 500}, 3661.5), (None, 0.0),
    ({"minutes": "{{ dose_minutes | int }}", "seconds": 5}, 5.0),      # template i scripts.yaml
    ({"minutes": None}, 0.0),
])
def test_varighed(værdi, forventet):
    assert ks.varighed(værdi) == forventet


def test_vindue_overlap_og_midnat():
    nat = ks.Vindue(22 * 60, 6 * 60)                  # krydser midnat
    morgen = ks.Vindue(5 * 60, 8 * 60)
    middag = ks.Vindue(11 * 60, 13 * 60)
    assert nat._intervaller() == [(22 * 60, 1440), (0, 6 * 60)]
    assert nat.overlapper(morgen)
    assert not nat.overlapper(middag)
    assert ks.Vindue().overlapper(middag)              # ubegrænset overlapper alt
    assert middag.overlapper(ks.Vindue())
    assert nat.beskriv() == "22:00–06:00"
    assert ks.Vindue().beskriv() == "all day"


def test_liste_tal_tid_hjælpere():
    assert ks._liste(None) == [] and ks._liste("a") == ["a"] and ks._liste(["a", "b"]) == ["a", "b"]
    assert ks._tal("3.5") == 3.5 and ks._tal("x") is None and ks._tal(None) is None
    assert ks._tid(45) == "45s" and ks._tid(600) == "10 min" and ks._tid(5400) == "1.5 h"


# ── Register ───────────────────────────────────────────────────────────────

def test_register_opløser_alle_tre_id_typer(reg):
    assert reg.opløs("light.stue_loft") == ["light.stue_loft"]
    assert reg.opløs("reg-stue-stik") == ["switch.stue_stik"]
    assert sorted(reg.opløs("dev-stue")) == ["light.stue_loft", "switch.stue_stik"]
    assert reg.opløs("dev-stue", "light") == ["light.stue_loft"]      # domænet snævrer ind
    assert sorted(reg.opløs("gang")) == ["binary_sensor.gang_bevaegelse", "light.gang"]
    assert reg.opløs("gang", "light") == ["light.gang"]
    assert reg.opløs(["reg-stue-loft", "light.gang"]) == ["light.stue_loft", "light.gang"]
    assert reg.opløs(None) == [] and reg.opløs("findes-ikke") == []


def test_register_navne_og_ukendte(reg):
    assert reg.navn("light.stue_loft") == "Loftlampe"
    assert reg.navn("switch.stue_stik") == "Stikkontakt"
    assert reg.navn("light.ukendt") == "light.ukendt"
    assert reg.device_navn["dev-gang"] == "Gangsensoren"
    assert reg.device_navn["dev-stue"] == "Stue Hue"
    assert reg.er_ukendt("light.ukendt") and not reg.er_ukendt("light.gang")
    assert reg.er_ukendt("hex-der-ikke-findes") and not reg.er_ukendt("dev-stue")


# ── Parser / indlæs ────────────────────────────────────────────────────────

def test_indlæs_læser_triggere_handlinger_grene_og_delay(reg, tmp_path):
    autos = [automation(
        "Gang lys",
        [state_trigger("binary_sensor.gang_bevaegelse", to="on", **{"for": "00:00:10"}),
         {"trigger": "time", "at": "22:00:00"}],
        [
            turn("light.turn_on", "light.gang"),
            {"delay": {"minutes": 2}},
            {"choose": [{"conditions": [], "sequence": [turn("light.turn_off", "light.gang")]}],
             "default": [turn("switch.turn_off", "dev-stue")]},
            {"action": "light.turn_on", "target": {"entity_id": "reg-stue-loft"}},
            turn("light.turn_on", "0badc0de0badc0de0badc0de0badc0de"),   # ukendt device/registry-id
        ],
        mode="restart",
    )]
    [a] = indlæs(reg, autos, tmp_path)
    assert a.alias == "Gang lys" and a.mode == "restart" and a.kilde == "automation"
    assert [s.art for s in a.signaler] == ["state", "time"]
    assert a.signaler[0].entitet == "binary_sensor.gang_bevaegelse"
    assert a.signaler[0].til == "on" and a.signaler[0].forsinkelse == 10.0
    assert a.signaler[0].beskriv() == "binary_sensor.gang_bevaegelse *→on for 10s"
    assert a.signaler[1].beskriv() == "at 22:00:00"
    skrivninger = {(w.entitet, w.effekt, w.gren, w.efter_forsinkelse) for w in a.skrivninger}
    assert ("light.gang", "tænd", (), 0.0) in skrivninger
    assert ("light.gang", "sluk", ("choose0#0",), 120.0) in skrivninger
    assert ("switch.stue_stik", "sluk", ("choose0#default",), 120.0) in skrivninger
    assert ("light.stue_loft", "tænd", (), 120.0) in skrivninger          # registry-id opløst
    # Kun uopløselige hex-id'er (device/registry/area) regnes som døde; et
    # entity_id med punktum tages for gode varer, fordi hjælpere ikke altid
    # står i entity registry (se Register.findes).
    assert a.ukendte == ["light.turn_on → 0badc0de0badc0de0badc0de0badc0de"]
    assert a.max_delay == 120.0
    assert a.rører == {"light.gang", "switch.stue_stik", "light.stue_loft"}
    assert a.lytter_på == {"binary_sensor.gang_bevaegelse"}


def test_indlæs_scripts_mapping_og_tidsvindue(reg, tmp_path):
    scripts = {"godnat": {"alias": "Godnat", "sequence": [turn("light.turn_off", "light.gang")]}}
    sti = tmp_path / "scripts.yaml"
    sti.write_text(yaml.safe_dump(scripts), encoding="utf-8")
    [s] = ks.indlæs(reg, sti, "script")
    assert s.id == "godnat" and s.alias == "Godnat" and s.kilde == "script"
    assert [w.effekt for w in s.skrivninger] == ["sluk"]

    vindue = indlæs(reg, [automation("Nat", [state_trigger("light.gang")], [],
                                     conditions=[{"condition": "time", "after": "22:00", "before": "06:00"}])],
                    tmp_path)[0].vindue
    assert (vindue.fra, vindue.til) == (22 * 60, 6 * 60)
    punkt = indlæs(reg, [automation("Kl 7", [{"trigger": "time", "at": "07:00"}], [])], tmp_path)[0].vindue
    assert (punkt.fra, punkt.til) == (7 * 60, 7 * 60 + 1)


# ── Analyse ────────────────────────────────────────────────────────────────

def test_analyse_finder_kortslutning_mellem_to_automationer(reg, tmp_path):
    autos = indlæs(reg, [
        automation("A", [state_trigger("light.gang")], [turn("light.turn_on", "light.stue_loft")], mode="restart"),
        automation("B", [state_trigger("light.stue_loft")], [turn("light.turn_off", "light.gang")]),
    ], tmp_path)
    fund = ks.Analyse(autos, reg).kortslutninger()
    assert len(fund) == 1
    assert fund[0].art == "kortslutning" and fund[0].alvor == "kritisk"
    assert set(fund[0].automationer) == {"A", "B"}
    assert any("light.stue_loft" in linje for linje in fund[0].spor)
    assert any("light.gang" in linje for linje in fund[0].spor)


def test_analyse_selvudløsning_modstrid_døde_og_delay(reg, tmp_path):
    autos = indlæs(reg, [
        automation("Selv", [state_trigger("light.gang")], [turn("light.toggle", "light.gang")], mode="queued"),
        automation("Tænd", [state_trigger("binary_sensor.gang_bevaegelse")], [turn("light.turn_on", "light.stue_loft")]),
        automation("Sluk", [state_trigger("binary_sensor.gang_bevaegelse")], [turn("light.turn_off", "light.stue_loft")]),
        automation("Død", [state_trigger("d00dd00dd00dd00dd00dd00dd00dd00d")], [turn("light.turn_on", "light.gang")]),
        automation("Langsom", [state_trigger("light.gang")],
                   [{"delay": {"minutes": 10}}, turn("switch.turn_off", "switch.stue_stik")]),
    ], tmp_path)
    a = ks.Analyse(autos, reg)

    [selv] = a.selvudløsning()
    assert selv.alvor == "kritisk" and selv.automationer == ["Selv"]

    modstrid = a.modstrid()
    assert len(modstrid) == 1 and modstrid[0].alvor == "advarsel"
    assert set(modstrid[0].automationer) == {"Tænd", "Sluk"}
    assert any("both are triggered by" in linje for linje in modstrid[0].spor)

    [død] = a.døde()
    assert død.automationer == ["Død"] and død.spor == ["trigger → d00dd00dd00dd00dd00dd00dd00dd00d"]

    [delay] = a.delay_fælde()
    assert delay.automationer == ["Langsom"] and delay.alvor == "advarsel"


def test_analyse_umulige_betingelser_og_skygger(reg, tmp_path):
    autos = indlæs(reg, [
        automation("Umulig", [state_trigger("light.gang")], [turn("light.turn_on", "light.stue_loft")],
                   conditions=[{"condition": "state", "entity_id": "light.gang", "state": "on"},
                               {"condition": "state", "entity_id": "light.gang", "state": "off"}]),
        automation("Tal", [state_trigger("light.gang")], [],
                   conditions=[{"condition": "numeric_state", "entity_id": "sensor.x", "above": 10, "below": 5}]),
        automation("Skygge 1", [state_trigger("light.gang")], [turn("light.turn_on", "light.stue_loft")]),
        automation("Skygge 2", [state_trigger("light.gang")], [turn("light.turn_off", "light.stue_loft")],
                   mode="parallel"),
    ], tmp_path)
    a = ks.Analyse(autos, reg)
    umulige = a.umulige()
    assert {f.automationer[0] for f in umulige} == {"Umulig", "Tal"}
    assert all(f.alvor == "kritisk" for f in umulige)

    [skygge] = a.skygger()
    assert set(skygge.automationer) == {"Umulig", "Skygge 1", "Skygge 2"}

    alle = a.alle()
    assert [f.alvor for f in alle] == sorted((f.alvor for f in alle), key=ks.ALVOR_RANG.__getitem__)


# ── Simulator ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("conditions", [
    [{"condition": "state", "entity_id": "light.gang", "state": ["on", "off"]}],
    [{"condition": "state", "entity_id": "light.gang", "state": ["on", "off"]},
     {"condition": "state", "entity_id": "light.gang", "state": "off"}],
    [{"condition": "state", "entity_id": "light.gang", "state": "on"},
     {"condition": "state", "entity_id": "light.gang", "attribute": "brightness", "state": "80"}],
    [{"condition": "state", "entity_id": "light.gang", "state": "on"},
     {"condition": "state", "entity_id": "light.gang", "state": "off", "enabled": False}],
    [{"condition": "state", "entity_id": "light.gang", "state": "on"},
     {"condition": "state", "entity_id": ["light.gang", "light.stue_loft"], "state": "off", "match": "any"}],
    [{"condition": "state", "entity_id": "light.gang", "state": "on"},
     {"condition": "state", "entity_id": "light.gang", "state": "input_select.expected_state"}],
    [{"condition": "numeric_state", "entity_id": "sensor.x", "above": 10, "below": 5, "enabled": False}],
])
def test_condition_heuristic_does_not_report_valid_or_unmodeled_cases(reg, tmp_path, conditions):
    autos = indlæs(reg, [automation("Synthetic valid condition", [], [], conditions=conditions)], tmp_path)
    assert ks.Analyse(autos, reg).umulige() == []

def test_simulator_opdager_ring_og_respekterer_tidsvindue(reg, tmp_path):
    autos = indlæs(reg, [
        automation("Ping", [state_trigger("light.gang", to="on")], [turn("light.turn_on", "light.stue_loft")],
                   mode="restart"),
        automation("Pong", [state_trigger("light.stue_loft", to="on")], [turn("light.turn_on", "light.gang")],
                   mode="restart"),
        automation("Kun om natten", [state_trigger("light.gang")], [turn("switch.turn_on", "switch.stue_stik")],
                   conditions=[{"condition": "time", "after": "23:00", "before": "05:00"}]),
    ], tmp_path)
    a = ks.Analyse(autos, reg)
    spor, udfald = ks.Simulator(a, reg).kør(ks.Hændelse(0.0, "light.gang", "on", "test", 0), 12 * 60)
    assert udfald.startswith("potential loop:")
    assert [e["auto"] for e in spor if e["art"] == "vågner"][:2] == ["Ping", "Pong"]
    assert any(e["art"] == "spring-over" and e["auto"] == "Kun om natten" for e in spor)

    spor2, udfald2 = ks.Simulator(a, reg).kør(ks.Hændelse(0.0, "sensor.ingen_lytter", "on", "test", 0), 0)
    assert spor2 == [] and udfald2 == "settled"


def test_simulator_numeric_state_og_forudsagt_tilstand(reg, tmp_path):
    autos = indlæs(reg, [
        automation("Varmt", [{"trigger": "numeric_state", "entity_id": "light.gang", "above": 20}],
                   [{"action": "input_number.set_value", "target": {"entity_id": "light.stue_loft"},
                     "data": {"value": 7}}]),
    ], tmp_path)
    a = ks.Analyse(autos, reg)
    sim = ks.Simulator(a, reg)
    assert sim.matcher(autos[0].signaler[0], ks.Hændelse(0, "light.gang", "25", "t", 0), 0)
    assert not sim.matcher(autos[0].signaler[0], ks.Hændelse(0, "light.gang", "15", "t", 0), 0)
    assert not sim.matcher(autos[0].signaler[0], ks.Hændelse(0, "light.gang", "varm", "t", 0), 0)
    assert ks.Simulator._tilstand(autos[0].skrivninger[0]) == "7"


# ── hent: token, WebSocket-rammer, API-dublet ──────────────────────────────

def test_ha_token_prioriterer_ks_variablen_og_ignorerer_tomme():
    assert ks.ha_token({"HASS_TOKEN": "c", "HOMEASSISTANT_TOKEN": "b", "KS_HA_TOKEN": "a"}) == "a"
    assert ks.ha_token({"KS_HA_TOKEN": "  ", "HOMEASSISTANT_TOKEN": "b"}) == "b"
    assert ks.ha_token({"HASS_TOKEN": "c"}) == "c"
    assert ks.ha_token({}) is None


@pytest.mark.parametrize("længde", [0, 5, 125, 126, 65535, 65536, 70000])
def test_ws_frame_og_parse_er_hinandens_modsætning(længde):
    payload = bytes(i % 251 for i in range(længde))
    ramme = ks.ws_frame(0x1, payload, mask=b"\x01\x02\x03\x04")
    forventet_hoved = 2 + (0 if længde < 126 else 2 if længde < 65536 else 8) + 4
    assert len(ramme) == forventet_hoved + længde
    assert ramme[0] == 0x81 and ramme[1] & 0x80             # FIN + tekst, maskeret
    fin, opcode, data, brugt = ks.ws_parse(ramme + b"rest")
    assert (fin, opcode, data, brugt) == (True, 0x1, payload, len(ramme))


def test_ws_parse_umaskeret_serverramme_og_ufuldstændig_buffer():
    payload = b"x" * 300
    server = bytes([0x81, 126]) + struct.pack(">H", 300) + payload   # server maskerer ikke
    assert ks.ws_parse(server) == (True, 0x1, payload, 4 + 300)
    assert ks.ws_parse(server[:-1]) is None
    assert ks.ws_parse(b"") is None and ks.ws_parse(bytes([0x81])) is None
    assert ks.ws_parse(bytes([0x81, 127, 0, 0])) is None          # 64-bit længde mangler


class _FalskSocket:
    """Leverer serverrammer i bidder og husker hvad klienten sendte."""

    def __init__(self, rammer: list[bytes], bid: int = 7) -> None:
        self.data = b"".join(rammer)
        self.bid = bid
        self.sendt: list[bytes] = []
        self.lukket = False

    def recv(self, n: int) -> bytes:
        ud, self.data = self.data[:self.bid], self.data[self.bid:]
        return ud

    def sendall(self, data: bytes) -> None:
        self.sendt.append(data)

    def close(self) -> None:
        self.lukket = True


def _server_ramme(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    return bytes([(0x80 if fin else 0) | opcode, len(payload)]) + payload


def test_websocket_samler_fragmenter_svarer_på_ping_og_kaster_ved_close():
    ws = ks.WebSocket(_FalskSocket([
        _server_ramme(0x1, b'{"a":', fin=False),
        _server_ramme(0x9, b"ping!"),                  # ping midt i en fragmenteret besked
        _server_ramme(0x0, b" 1}"),
        _server_ramme(0x8, b""),
    ]))
    assert ws.modtag_tekst() == '{"a": 1}'
    pong = ws.sock.sendt[0]
    assert pong[0] == 0x8A and ks.ws_parse(pong)[2] == b"ping!"
    with pytest.raises(OSError, match="closed"):
        ws.modtag_tekst()
    ws.luk()
    assert ws.sock.lukket


def test_ha_api_ws_list_autentificerer_og_afviser_fejl(monkeypatch):
    sendt: list[dict] = []

    class Dublet(ks.WebSocket):
        def __init__(self, svar):
            self.svar = list(svar)
            self.lukket = False

        def send_tekst(self, tekst):
            sendt.append(json.loads(tekst))

        def modtag_tekst(self):
            return json.dumps(self.svar.pop(0))

        def luk(self):
            self.lukket = True

    ok = Dublet([
        {"type": "auth_required"}, {"type": "auth_ok"},
        {"id": 1, "type": "event"},                                        # støj ignoreres
        {"id": 1, "type": "result", "success": True, "result": [{"id": "d"}]},
        {"id": 2, "type": "result", "success": True, "result": [{"entity_id": "light.x"}]},
    ])
    monkeypatch.setattr(ks.WebSocket, "forbind", classmethod(lambda cls, url, timeout: ok))
    api = ks.HaApi("http://ha.test:8123/", "hemmeligt-token")
    assert api.ws_list("config/device_registry/list", "config/entity_registry/list") == {
        "config/device_registry/list": [{"id": "d"}],
        "config/entity_registry/list": [{"entity_id": "light.x"}],
    }
    assert sendt[0] == {"type": "auth", "access_token": "hemmeligt-token"}
    assert sendt[1:] == [{"id": 1, "type": "config/device_registry/list"},
                         {"id": 2, "type": "config/entity_registry/list"}]
    assert ok.lukket

    afvist = Dublet([{"type": "auth_required"}, {"type": "auth_invalid"}])
    monkeypatch.setattr(ks.WebSocket, "forbind", classmethod(lambda cls, url, timeout: afvist))
    with pytest.raises(OSError, match="rejected the token"):
        api.ws_list("config/device_registry/list")
    assert afvist.lukket

    fejl = Dublet([{"type": "auth_required"}, {"type": "auth_ok"},
                   {"id": 1, "type": "result", "success": False, "error": {"message": "nej"}}])
    monkeypatch.setattr(ks.WebSocket, "forbind", classmethod(lambda cls, url, timeout: fejl))
    with pytest.raises(OSError, match="nej"):
        api.ws_list("config/device_registry/list")


class _FalskHa:
    """Dublet for HaApi: samme to metoder, ingen netværk."""

    def __init__(self, *, scripts_404: bool = False, fejl_efter: int | None = None) -> None:
        self.scripts_404 = scripts_404
        self.fejl_efter = fejl_efter
        self.kald: list[str] = []

    def ws_list(self, *typer):
        self.kald.append("ws:" + ",".join(typer))
        return {"config/device_registry/list": ENHEDER, "config/entity_registry/list": ENTITETER}

    def get(self, sti):
        self.kald.append(sti)
        if self.fejl_efter is not None and len(self.kald) > self.fejl_efter:
            raise urllib.error.URLError("forbindelsen forsvandt")
        if sti == "/api/states":
            return [
                {"entity_id": "automation.gang", "attributes": {"id": "1700000000001"}},
                {"entity_id": "automation.uden_id", "attributes": {}},
                {"entity_id": "automation.pakke", "attributes": {"id": "pool_vagt"}},   # YAML-pakke: 404
                {"entity_id": "script.godnat", "attributes": {}},
                {"entity_id": "light.gang", "attributes": {}},
            ]
        if sti == "/api/config/automation/config/pool_vagt":
            raise urllib.error.HTTPError(sti, 404, "Not Found", {}, None)
        if sti == "/api/config/automation/config/1700000000001":
            return {"alias": "Gang lys", "mode": "single",
                    "triggers": [state_trigger("binary_sensor.gang_bevaegelse", to="on")],
                    "actions": [turn("light.turn_on", "light.gang")]}
        if sti == "/api/config/script/config/godnat":
            if self.scripts_404:
                raise urllib.error.HTTPError(sti, 404, "Not Found", {}, None)
            return {"alias": "Godnat", "sequence": [turn("light.turn_off", "light.gang")]}
        raise AssertionError(f"uventet kald {sti}")


def test_hent_via_api_skriver_alle_fire_filer_i_det_kendte_format(tmp_path):
    cache = tmp_path / "cache"
    linjer: list[str] = []
    ha = _FalskHa()
    assert ks.hent_via_api(ha, cache, skriv=linjer.append) == 0
    assert ha.kald[0] == "ws:config/device_registry/list,config/entity_registry/list"
    assert "/api/config/automation/config/1700000000001" in ha.kald
    assert "/api/config/automation/config/pool_vagt" in ha.kald        # 404 → sprunget over
    assert not any("uden_id" in k for k in ha.kald)

    autos = yaml.safe_load((cache / "automations.yaml").read_text(encoding="utf-8"))
    assert autos == [{"alias": "Gang lys", "mode": "single",
                      "triggers": [state_trigger("binary_sensor.gang_bevaegelse", to="on")],
                      "actions": [turn("light.turn_on", "light.gang")], "id": "1700000000001"}]
    scripts = yaml.safe_load((cache / "scripts.yaml").read_text(encoding="utf-8"))
    assert list(scripts) == ["godnat"]
    dev = json.loads((cache / "device_registry.json").read_text(encoding="utf-8"))
    ent = json.loads((cache / "entity_registry.json").read_text(encoding="utf-8"))
    assert dev["key"] == "core.device_registry" and dev["data"]["devices"] == ENHEDER
    assert ent["key"] == "core.entity_registry" and ent["data"]["entities"] == ENTITETER
    assert not list(cache.glob(".*.tmp"))
    assert len(linjer) == 5 and linjer[-1].strip().startswith("1 automation · 1 script")
    assert "1 YAML automation" in linjer[-1]

    # The generated snapshot can be read directly by the analyzer.
    reg = ks.Register(dev["data"]["devices"], ent["data"]["entities"])
    autos_obj = ks.indlæs(reg, cache / "automations.yaml", "automation")
    autos_obj += ks.indlæs(reg, cache / "scripts.yaml", "script")
    assert [a.alias for a in autos_obj] == ["Gang lys", "Godnat"]
    # Scriptet (sequence) tæller nu som skriver: tænd/sluk af light.gang → én modstrid-info.
    fund = ks.Analyse(autos_obj, reg).alle()
    assert [(f.art, f.alvor, sorted(f.automationer)) for f in fund] == [("modstrid", "info", ["Gang lys", "Godnat"])]


def test_hent_via_api_springer_yaml_scripts_over_ved_404(tmp_path):
    cache = tmp_path / "cache"
    assert ks.hent_via_api(_FalskHa(scripts_404=True), cache, skriv=lambda s: None) == 0
    assert yaml.safe_load((cache / "scripts.yaml").read_text(encoding="utf-8")) == {}


def test_hent_via_api_skriver_intet_når_et_kald_fejler(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "automations.yaml").write_text("- id: gammel\n", encoding="utf-8")
    with pytest.raises(urllib.error.URLError):
        ks.hent_via_api(_FalskHa(fejl_efter=2), cache, skriv=lambda s: None)
    assert (cache / "automations.yaml").read_text(encoding="utf-8") == "- id: gammel\n"
    assert not (cache / "entity_registry.json").exists()


def test_hent_requires_token_then_uses_api(monkeypatch):
    kaldt: list[str] = []
    monkeypatch.setattr(ks, "hent_via_api", lambda api: kaldt.append(f"api:{api.url}") or 0)
    monkeypatch.setattr(ks, "HA_URL", "http://ha.test:8123")
    for navn in ks.HA_TOKEN_VARS:
        monkeypatch.delenv(navn, raising=False)
    assert ks.hent() == 1 and kaldt == []
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "t")
    assert ks.hent() == 0 and kaldt[-1] == f"api:{ks.HA_URL}"


def test_hent_oversætter_http_og_netværksfejl_til_exit_1(monkeypatch, capsys):
    monkeypatch.setenv("KS_HA_TOKEN", "t")
    monkeypatch.setattr(ks, "HA_URL", "http://ha.test:8123")

    def http_fejl(api):
        raise urllib.error.HTTPError("http://ha/api/states", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(ks, "hent_via_api", http_fejl)
    assert ks.hent() == 1
    assert "HTTP 401" in capsys.readouterr().err

    def net_fejl(api):
        raise OSError("ingen rute")

    monkeypatch.setattr(ks, "hent_via_api", net_fejl)
    assert ks.hent() == 1
    assert "ingen rute" in capsys.readouterr().err


def test_cache_alder_læser_stempel_først(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "CACHE", tmp_path)
    assert ks.cache_alder_dage() is None
    (tmp_path / "device_registry.json").write_text("{}", encoding="utf-8")
    assert ks.cache_alder_dage() < 0.01
    (tmp_path / ".smartbolig-refresh.json").write_text(json.dumps({"completed_at": 0}), encoding="utf-8")
    assert ks.cache_alder_dage() > 365 * 50          # 1970 → mange år


def test_skriv_atomisk_efterlader_ingen_temp_fil(tmp_path):
    mål = tmp_path / "ny" / "fil.json"
    ks._skriv_atomisk(mål, b"abc")
    assert mål.read_bytes() == b"abc"
    assert os.listdir(mål.parent) == ["fil.json"]
