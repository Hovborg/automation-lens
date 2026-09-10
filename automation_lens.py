#!/usr/bin/env python3
"""Automation Lens: read-only static analysis of Home Assistant automations.

Resolves entity/device references and reports potential loops, conflicting
writes, missing references and delay traps. Findings describe a simplified
model of exported configuration; they are not proof of runtime behavior.

Try: automation-lens --data-dir examples/demo --findings
Legacy Danish CLI aliases remain available for existing scripts.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

if os.name == "nt":
    # Windows may use a legacy locale encoding when stdout is redirected.
    # UTF-8 keeps entity names and report symbols intact in pipes and files.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("KS_DATA_DIR", "cache")).expanduser()
# A connection must be explicitly configured. Offline analysis never connects.
HA_URL = (
    os.environ.get("KS_HA_URL")
    or os.environ.get("HOMEASSISTANT_URL")
    or os.environ.get("HASS_SERVER")
    or ""
).rstrip("/")
HA_TOKEN_VARS = ("KS_HA_TOKEN", "HOMEASSISTANT_TOKEN", "HASS_TOKEN")

class C:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def __getattr__(self, n: str) -> str:
        k = {"reset": "0", "bold": "1", "dim": "2", "italic": "3",
             "red": "31", "green": "32", "yellow": "33", "blue": "34",
             "grey": "90", "brred": "91", "brgreen": "92", "bryellow": "93",
             "brblue": "94", "brmagenta": "95", "brcyan": "96"}
        return f"\033[{k[n]}m" if C.on else ""


c = C()


# ═══════════════════════════════════════════════════════════════ registre ══

class Register:
    """Resolve the devices and entities known to Home Assistant.

    Automation YAML can mix entity IDs, device IDs and entity registry entry
    IDs. They must be resolved separately for the analysis to remain accurate.
    """

    def __init__(self, devices: list[dict], entities: list[dict]) -> None:
        self.entity_af_regid: dict[str, str] = {}
        self.entiteter: dict[str, dict] = {}
        self.per_device: dict[str, list[str]] = defaultdict(list)
        self.per_area: dict[str, list[str]] = defaultdict(list)
        self.enheder: dict[str, dict] = {d["id"]: d for d in devices}
        area_af_device = {d["id"]: d.get("area_id") for d in devices}
        navn_af_device = {
            d["id"]: (d.get("name_by_user") or d.get("name") or d["id"][:8])
            for d in devices
        }
        self.device_navn = navn_af_device

        for e in entities:
            eid = e.get("entity_id")
            if not eid:
                continue
            self.entiteter[eid] = e
            if e.get("id"):
                self.entity_af_regid[e["id"]] = eid
            dev = e.get("device_id")
            if dev:
                self.per_device[dev].append(eid)
            area = e.get("area_id") or (area_af_device.get(dev) if dev else None)
            if area:
                self.per_area[area].append(eid)

    def findes(self, eid: str) -> bool:
        # Hjælpere (input_*, timer, counter…) og skabte entiteter står ikke
        # altid i entity registry, så domænet alene kan ikke afgøre det.
        return eid in self.entiteter

    def navn(self, eid: str) -> str:
        e = self.entiteter.get(eid)
        if not e:
            return eid
        return e.get("name") or e.get("original_name") or eid

    def opløs(self, værdi, domæne: str | None = None) -> list[str]:
        """Resolve one string or list reference to concrete entity IDs."""
        if værdi is None:
            return []
        if isinstance(værdi, (list, tuple)):
            ud = []
            for v in værdi:
                ud += self.opløs(v, domæne)
            return ud
        s = str(værdi)
        if "." in s:
            return [s]
        if s in self.entity_af_regid:                       # registry entry id
            return [self.entity_af_regid[s]]
        if s in self.per_device:                            # device id
            kandidater = self.per_device[s]
            if domæne:
                snævert = [e for e in kandidater if e.startswith(domæne + ".")]
                if snævert:
                    return snævert
            return kandidater
        if s in self.per_area:                              # area id
            kandidater = self.per_area[s]
            if domæne:
                snævert = [e for e in kandidater if e.startswith(domæne + ".")]
                if snævert:
                    return snævert
            return kandidater
        return []                                            # ukendt reference

    def er_ukendt(self, værdi) -> bool:
        s = str(værdi)
        if "." in s:
            return not self.findes(s)
        return (s not in self.entity_af_regid and s not in self.per_device
                and s not in self.per_area)


# ════════════════════════════════════════════════════════════════ model ══

TÆND = {"turn_on", "on", "open_cover", "open", "unlock", "start", "select_next",
        "media_play", "arm_home", "arm_away", "press", "increment"}
SLUK = {"turn_off", "off", "close_cover", "close", "lock", "stop", "media_pause",
        "media_stop", "disarm", "cancel", "decrement"}
SKIFT = {"toggle", "toggle_cover"}


@dataclass
class Signal:
    """A signal that can trigger an automation."""
    art: str                      # state, numeric_state, time, device, event …
    entitet: str | None = None
    fra: str | None = None
    til: str | None = None
    over: float | None = None
    under: float | None = None
    tidspunkt: str | None = None
    forsinkelse: float = 0.0      # `for:` i sekunder
    rå: dict = field(default_factory=dict)

    def beskriv(self) -> str:
        if self.art == "state":
            d = f"{self.entitet}"
            if self.fra or self.til:
                d += f" {self.fra or '*'}→{self.til or '*'}"
            if self.forsinkelse:
                d += f" for {int(self.forsinkelse)}s"
            return d
        if self.art == "numeric_state":
            g = []
            if self.over is not None:
                g.append(f">{self.over}")
            if self.under is not None:
                g.append(f"<{self.under}")
            return f"{self.entitet} {' and '.join(g)}"
        if self.art == "time":
            return f"at {self.tidspunkt}"
        return self.art + (f" {self.entitet}" if self.entitet else "")


@dataclass
class Skrivning:
    """An action that changes an entity."""
    entitet: str
    service: str
    effekt: str                   # tænd | sluk | skift | sæt | ukendt
    gren: tuple = ()              # hvilken choose/if-gren, tom = altid
    efter_forsinkelse: float = 0.0
    rå: dict = field(default_factory=dict)


@dataclass
class Vindue:
    """A time-of-day interval. None means unrestricted."""
    fra: int | None = None        # minutter siden midnat
    til: int | None = None

    def overlapper(self, andet: "Vindue") -> bool:
        if self.fra is None or andet.fra is None:
            return True
        a = self._intervaller()
        b = andet._intervaller()
        return any(x[0] < y[1] and y[0] < x[1] for x in a for y in b)

    def _intervaller(self) -> list[tuple[int, int]]:
        f, t = self.fra, self.til
        if f is None or t is None:
            return [(0, 1440)]
        if f <= t:
            return [(f, t)]
        return [(f, 1440), (0, t)]                # vinduet krydser midnat

    def beskriv(self) -> str:
        if self.fra is None:
            return "all day"
        return f"{self.fra // 60:02d}:{self.fra % 60:02d}–{self.til // 60:02d}:{self.til % 60:02d}"


@dataclass
class Automation:
    id: str
    alias: str
    mode: str
    signaler: list[Signal]
    skrivninger: list[Skrivning]
    vindue: Vindue
    betingelser: list[dict]
    ukendte: list[str]                       # referencer der ikke findes
    max_delay: float
    kilde: str = ""

    @property
    def lytter_på(self) -> set[str]:
        return {s.entitet for s in self.signaler if s.entitet}

    @property
    def rører(self) -> set[str]:
        return {w.entitet for w in self.skrivninger}


def tid_til_min(v) -> int | None:
    if not isinstance(v, str):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", v.strip())
    if not m:
        return None
    return (int(m.group(1)) % 24) * 60 + int(m.group(2))


def varighed(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.match(r"^(\d+):(\d+):(\d+)", v)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        try:
            return float(v)
        except ValueError:
            return 0.0
    if isinstance(v, dict):
        # Felterne kan være templates ("{{ dose_minutes | int }}"); de tæller som 0.
        tal = {k: (_tal(v.get(k, 0)) or 0.0) for k in ("hours", "minutes", "seconds", "milliseconds")}
        return tal["hours"] * 3600 + tal["minutes"] * 60 + tal["seconds"] + tal["milliseconds"] / 1000
    return 0.0


class Parser:
    def __init__(self, reg: Register) -> None:
        self.reg = reg

    # ── triggere ──────────────────────────────────────────────────────────
    def signaler(self, auto: dict) -> tuple[list[Signal], list[str]]:
        ud, ukendte = [], []
        rå = auto.get("triggers") or auto.get("trigger") or []
        if isinstance(rå, dict):
            rå = [rå]
        for t in rå:
            if not isinstance(t, dict):
                continue
            art = t.get("platform") or t.get("trigger") or "?"
            if art in ("state", "numeric_state", "device"):
                ref = t.get("entity_id")
                if ref is None and art == "device":
                    ref = t.get("entity_id") or t.get("device_id")
                mål = self.reg.opløs(ref, t.get("domain"))
                if ref is not None and not mål and self.reg.er_ukendt(
                        ref if isinstance(ref, str) else (ref or [""])[0]):
                    ukendte.append(f"trigger → {ref}")
                for e in mål or []:
                    ud.append(Signal(
                        art="numeric_state" if art == "numeric_state" else "state",
                        entitet=e, fra=str(t["from"]) if t.get("from") is not None else None,
                        til=str(t["to"]) if t.get("to") is not None else None,
                        over=_tal(t.get("above")), under=_tal(t.get("below")),
                        forsinkelse=varighed(t.get("for")), rå=t))
            elif art == "time":
                for a in _liste(t.get("at")):
                    ud.append(Signal(art="time", tidspunkt=str(a), rå=t))
            else:
                ud.append(Signal(art=art, rå=t))
        return ud, ukendte

    # ── betingelser ───────────────────────────────────────────────────────
    def vindue(self, auto: dict) -> Vindue:
        """Return the narrowest time window in which the automation can run.

        Only top-level time conditions count. A condition inside an OR branch
        does not constrain the entire automation.
        """
        v = Vindue()
        for cond in _liste(auto.get("conditions") or auto.get("condition")):
            if not isinstance(cond, dict):
                continue
            if (cond.get("condition") or cond.get("type")) != "time":
                continue
            f, t = tid_til_min(cond.get("after")), tid_til_min(cond.get("before"))
            if f is not None or t is not None:
                v = Vindue(f if f is not None else 0, t if t is not None else 1440)
        # Et rent tidstrigger uden tidsbetingelse er også et punkt på døgnet.
        if v.fra is None:
            tider = [tid_til_min(s.tidspunkt) for s in self.signaler(auto)[0]
                     if s.art == "time"]
            tider = [t for t in tider if t is not None]
            if tider and len(tider) == 1:
                v = Vindue(tider[0], min(1440, tider[0] + 1))
        return v

    # ── handlinger ────────────────────────────────────────────────────────
    def skrivninger(self, auto: dict) -> tuple[list[Skrivning], list[str], float]:
        ud: list[Skrivning] = []
        ukendte: list[str] = []
        max_delay = 0.0

        def gå(trin_liste, gren: tuple, forsinket: float) -> float:
            nonlocal max_delay
            akkumuleret = forsinket
            for trin in _liste(trin_liste):
                if not isinstance(trin, dict):
                    continue
                if "delay" in trin:
                    akkumuleret += varighed(trin["delay"])
                    max_delay = max(max_delay, akkumuleret)
                    continue
                if "wait_for_trigger" in trin or "wait_template" in trin:
                    t = varighed(trin.get("timeout")) or 300.0
                    akkumuleret += t
                    max_delay = max(max_delay, akkumuleret)
                    continue
                # Grene er gensidigt udelukkende: de må aldrig regnes som
                # samtidige skrivninger, ellers er hver choose en falsk alarm.
                if "choose" in trin:
                    for i, mulighed in enumerate(_liste(trin["choose"])):
                        if isinstance(mulighed, dict):
                            gå(mulighed.get("sequence"), gren + (f"choose{len(gren)}#{i}",),
                               akkumuleret)
                    if trin.get("default"):
                        gå(trin["default"], gren + (f"choose{len(gren)}#default",), akkumuleret)
                    continue
                if "if" in trin:
                    gå(trin.get("then"), gren + (f"if{len(gren)}#ja",), akkumuleret)
                    if trin.get("else"):
                        gå(trin["else"], gren + (f"if{len(gren)}#nej",), akkumuleret)
                    continue
                if "repeat" in trin and isinstance(trin["repeat"], dict):
                    gå(trin["repeat"].get("sequence"), gren + ("repeat",), akkumuleret)
                    continue
                for nøgle in ("sequence", "parallel"):
                    if nøgle in trin:
                        gå(trin[nøgle], gren, akkumuleret)
                        break
                else:
                    self._skriv(trin, gren, akkumuleret, ud, ukendte)
            return akkumuleret

        # Automationer har actions/action; scripts (scripts.yaml) har sequence.
        gå(auto.get("actions") or auto.get("action") or auto.get("sequence"), (), 0.0)
        return ud, ukendte, max_delay

    def _skriv(self, trin: dict, gren: tuple, forsinket: float,
               ud: list[Skrivning], ukendte: list[str]) -> None:
        service = trin.get("action") or trin.get("service")
        if not service and trin.get("domain") and trin.get("type"):
            service = f"{trin['domain']}.{trin['type']}"          # device_action
        if not service or not isinstance(service, str):
            return
        domæne = service.split(".")[0]
        verbum = service.split(".")[-1]
        effekt = ("tænd" if verbum in TÆND else "sluk" if verbum in SLUK
                  else "skift" if verbum in SKIFT else "sæt")

        mål = trin.get("target") or {}
        refs = []
        for nøgle in ("entity_id", "device_id", "area_id"):
            refs += _liste(mål.get(nøgle))
            refs += _liste(trin.get(nøgle))
        if not refs:
            data = trin.get("data") or {}
            refs += _liste(data.get("entity_id"))
        for ref in refs:
            entiteter = self.reg.opløs(ref, domæne)
            if self.reg.er_ukendt(ref):
                ukendte.append(f"{service} → {ref}")
                continue
            for e in entiteter:
                ud.append(Skrivning(e, service, effekt, gren, forsinket, trin))


def _liste(v):
    if v is None:
        return []
    return v if isinstance(v, (list, tuple)) else [v]


def _tal(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def indlæs(reg: Register, sti: Path, kilde: str) -> list[Automation]:
    data = yaml.safe_load(sti.read_text(encoding="utf-8")) or []
    if isinstance(data, dict):                       # scripts.yaml er en mapping
        data = [{**v, "id": k, "alias": v.get("alias", k)} for k, v in data.items()
                if isinstance(v, dict)]
    p = Parser(reg)
    ud = []
    for a in data:
        if not isinstance(a, dict):
            continue
        sig, u1 = p.signaler(a)
        skr, u2, delay = p.skrivninger(a)
        ud.append(Automation(
            id=str(a.get("id", "?")), alias=a.get("alias") or str(a.get("id", "?")),
            mode=a.get("mode", "single"), signaler=sig, skrivninger=skr,
            vindue=p.vindue(a),
            betingelser=_liste(a.get("conditions") or a.get("condition")),
            ukendte=u1 + u2, max_delay=delay, kilde=kilde))
    return ud


# ════════════════════════════════════════════════════════════════ analyse ══

@dataclass
class Fund:
    art: str
    alvor: str                    # kritisk | advarsel | info
    overskrift: str
    spor: list[str]               # modeksemplet, linje for linje
    automationer: list[str] = field(default_factory=list)


ALVOR_RANG = {"kritisk": 0, "advarsel": 1, "info": 2}
FINDING_KINDS = {
    "kortslutning": "feedback-loop",
    "selvudløsning": "self-trigger",
    "modstrid": "conflicting-write",
    "død-reference": "missing-reference",
    "delay-fælde": "delay-trap",
    "umulig-betingelse": "impossible-condition",
    "skygge": "overlapping-automation",
}
SEVERITIES = {"kritisk": "critical", "advarsel": "warning", "info": "info"}
EFFECTS = {"tænd": "turn on", "sluk": "turn off", "skift": "toggle", "sæt": "set"}


def eksportér_fund(fund: Fund) -> dict:
    """Return one finding using the stable, English public JSON schema."""
    return {
        "kind": FINDING_KINDS[fund.art],
        "severity": SEVERITIES[fund.alvor],
        "title": fund.overskrift,
        "trace": fund.spor,
        "automations": fund.automationer,
    }


def intern_art(værdi: str | None) -> str | None:
    """Map an English kind to its internal name; accept legacy names unchanged."""
    if værdi is None:
        return None
    return {public: intern for intern, public in FINDING_KINDS.items()}.get(værdi, værdi)


class Analyse:
    def __init__(self, autos: list[Automation], reg: Register) -> None:
        self.a = autos
        self.reg = reg
        self.skrivere: dict[str, list[Automation]] = defaultdict(list)
        self.lyttere: dict[str, list[Automation]] = defaultdict(list)
        for x in autos:
            for w in x.skrivninger:
                self.skrivere[w.entitet].append(x)
            for e in x.lytter_på:
                self.lyttere[e].append(x)

    # ── 1. kortslutninger ────────────────────────────────────────────────
    def kortslutninger(self) -> list[Fund]:
        """Find cycles in the graph of writes and matching entity triggers.

        Each edge identifies an entity written by one automation and observed
        by another. A cycle is reported as a potential feedback loop.
        """
        kant: dict[str, dict[str, str]] = defaultdict(dict)
        for x in self.a:
            for w in x.skrivninger:
                for y in self.lyttere.get(w.entitet, []):
                    if y is not x:
                        kant[x.alias].setdefault(y.alias, w.entitet)

        fund, sete = [], set()
        for start in kant:
            for cyklus in self._cykler(kant, start, [start], set([start]), 5):
                nøgle = frozenset(cyklus)
                if nøgle in sete:
                    continue
                sete.add(nøgle)
                spor = []
                for i in range(len(cyklus)):
                    a, b = cyklus[i], cyklus[(i + 1) % len(cyklus)]
                    spor.append(f"{a}  ──[{kant[a][b]}]──▶  {b}")
                moder = [x.mode for x in self.a if x.alias in cyklus]
                farlig = any(m in ("restart", "queued", "parallel") for m in moder)
                fund.append(Fund(
                    "kortslutning", "kritisk" if farlig else "advarsel",
                    f"Potential feedback loop between {len(cyklus)} automations"
                    + (" (at least one uses restart/queued mode, so repeated runs are possible)"
                       if farlig else " (all use single mode, which may limit repeated runs)"),
                    spor, list(cyklus)))
        return fund

    def _cykler(self, kant, node, sti, i_sti, maks):
        if len(sti) > maks:
            return
        for næste in kant.get(node, {}):
            if næste == sti[0] and len(sti) > 1:
                yield list(sti)
            elif næste not in i_sti:
                sti.append(næste)
                i_sti.add(næste)
                yield from self._cykler(kant, næste, sti, i_sti, maks)
                sti.pop()
                i_sti.discard(næste)

    # ── 2. selvudløsning ─────────────────────────────────────────────────
    def selvudløsning(self) -> list[Fund]:
        ud = []
        for x in self.a:
            fælles = x.lytter_på & x.rører
            if not fælles:
                continue
            alvor = "kritisk" if x.mode in ("restart", "queued", "parallel") else "advarsel"
            for e in sorted(fælles)[:3]:
                sig = next((s for s in x.signaler if s.entitet == e), None)
                skr = next((w for w in x.skrivninger if w.entitet == e), None)
                ud.append(Fund(
                    "selvudløsning", alvor,
                    f"{x.alias} may be triggered by {e} and write to the same entity",
                    [f"trigger: {sig.beskriv() if sig else e}",
                     f"writes:  {skr.service} → {e} ({EFFECTS[skr.effekt]})" if skr else "",
                     f"mode:    {x.mode}"
                     + ("  ← may restart itself" if alvor == "kritisk" else
                        "  ← new runs are dropped while it is active")],
                    [x.alias]))
        return ud

    # ── 3. modstridende skrivninger ──────────────────────────────────────
    def modstrid(self) -> list[Fund]:
        ud = []
        for entitet, skrivere in sorted(self.skrivere.items()):
            if len(skrivere) < 2:
                continue
            par = set()
            for i, x in enumerate(skrivere):
                for y in skrivere[i + 1:]:
                    if x is y or (x.alias, y.alias) in par:
                        continue
                    par.add((x.alias, y.alias))
                    ex = {w.effekt for w in x.skrivninger if w.entitet == entitet}
                    ey = {w.effekt for w in y.skrivninger if w.entitet == entitet}
                    if not (("tænd" in ex and "sluk" in ey)
                            or ("sluk" in ex and "tænd" in ey)):
                        continue
                    if not x.vindue.overlapper(y.vindue):
                        continue
                    delt = x.lytter_på & y.lytter_på
                    alvor = "advarsel" if delt else "info"
                    spor = [
                        f"{x.alias}  ({x.vindue.beskriv()})",
                        f"    {'/'.join(EFFECTS[e] for e in sorted(ex))} → {entitet}",
                        f"{y.alias}  ({y.vindue.beskriv()})",
                        f"    {'/'.join(EFFECTS[e] for e in sorted(ey))} → {entitet}",
                    ]
                    if delt:
                        spor.append(f"both are triggered by: {', '.join(sorted(delt)[:3])}"
                                    "  ← the same event may match both")
                    ud.append(Fund(
                        "modstrid", alvor,
                        f"Two automations may push {entitet} in opposite directions",
                        spor, [x.alias, y.alias]))
        return ud

    # ── 4. døde referencer ───────────────────────────────────────────────
    def døde(self) -> list[Fund]:
        ud = []
        for x in self.a:
            if not x.ukendte:
                continue
            ud.append(Fund(
                "død-reference", "advarsel",
                f"{x.alias} references {_antal(len(x.ukendte), 'item')} missing from this snapshot",
                [f"{r}" for r in x.ukendte[:6]]
                + ([f"… and {len(x.ukendte) - 6} more"] if len(x.ukendte) > 6 else []),
                [x.alias]))
        return ud

    # ── 5. delay-fælden ──────────────────────────────────────────────────
    def delay_fælde(self) -> list[Fund]:
        """Find single-mode automations whose long runs may drop new triggers."""
        ud = []
        for x in self.a:
            if x.mode != "single" or x.max_delay < 60:
                continue
            hyppig = any(s.art in ("state", "numeric_state", "time_pattern", "mqtt", "event")
                         for s in x.signaler)
            if not hyppig:
                continue
            ud.append(Fund(
                "delay-fælde", "advarsel" if x.max_delay >= 300 else "info",
                f"{x.alias} may remain active for up to {_tid(x.max_delay)}",
                [f"mode: single, longest run: {_tid(x.max_delay)}",
                 "triggered by: " + ", ".join(s.beskriv() for s in x.signaler[:3]),
                 "triggers during that window are silently dropped (only a warning is logged)"],
                [x.alias]))
        return ud

    # ── 6. umulige betingelser ───────────────────────────────────────────
    def umulige(self) -> list[Fund]:
        ud = []
        for x in self.a:
            krav: dict[str, set] = defaultdict(set)
            for cond in x.betingelser:
                if not isinstance(cond, dict):
                    continue
                if (cond.get("condition") or cond.get("type")) != "state":
                    continue
                # A list is OR within one state condition. Attribute tests,
                # match:any and dynamic values need a richer model; skip them.
                state = cond.get("state")
                if (cond.get("enabled", True) is not True or cond.get("attribute")
                        or cond.get("match", "all") != "all" or not isinstance(state, str)
                        or "{{" in state or "{%" in state
                        or state.startswith(("input_select.", "input_text."))):
                    continue
                for e in self.reg.opløs(cond.get("entity_id")):
                    krav[e].add(state)
            for e, s in krav.items():
                if len(s) > 1:
                    ud.append(Fund(
                        "umulig-betingelse", "kritisk",
                        f"{x.alias} has mutually exclusive conditions",
                        [f"{e} must be both: {' and '.join(sorted(s))}",
                         "the conditions are combined with AND and exclude one another"],
                        [x.alias]))
            for cond in x.betingelser:
                if not isinstance(cond, dict):
                    continue
                if (cond.get("condition") or cond.get("type")) != "numeric_state":
                    continue
                if cond.get("enabled", True) is not True:
                    continue
                o, u = _tal(cond.get("above")), _tal(cond.get("below"))
                if o is not None and u is not None and o >= u:
                    ud.append(Fund(
                        "umulig-betingelse", "kritisk",
                        f"{x.alias} has mutually exclusive conditions",
                        [f"{cond.get('entity_id')} must be >{o} and <{u} at the same time"],
                        [x.alias]))
        return ud

    # ── 7. skygger ───────────────────────────────────────────────────────
    def skygger(self) -> list[Fund]:
        efter_signatur: dict[tuple, list[Automation]] = defaultdict(list)
        for x in self.a:
            if not x.signaler or not x.rører:
                continue
            sig = (frozenset(s.beskriv() for s in x.signaler), frozenset(x.rører))
            efter_signatur[sig].append(x)
        ud = []
        for (_, rører), gruppe in efter_signatur.items():
            if len(gruppe) < 2:
                continue
            ud.append(Fund(
                "skygge", "info",
                f"{_antal(len(gruppe), 'automation')} have the same trigger and write to the same entities",
                [f"· {x.alias}  (mode: {x.mode})" for x in gruppe]
                + [f"writes to: {', '.join(sorted(rører)[:4])}"],
                [x.alias for x in gruppe]))
        return ud

    def alle(self) -> list[Fund]:
        f = (self.kortslutninger() + self.selvudløsning() + self.umulige()
             + self.modstrid() + self.døde() + self.delay_fælde() + self.skygger())
        f.sort(key=lambda x: (ALVOR_RANG[x.alvor], x.art, x.overskrift))
        return f


def _tid(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


def _antal(antal: int, ental: str, flertal: str | None = None) -> str:
    """Format a count with a simple English singular or plural noun."""
    return f"{antal} {ental if antal == 1 else (flertal or ental + 's')}"


# ═════════════════════════════════════════════════════════════ simulator ══

@dataclass
class Hændelse:
    tid: float                    # sekunder efter t0
    entitet: str
    tilstand: str
    årsag: str                    # hvem forårsagede den
    dybde: int


class Simulator:
    """Run a what-if event through the simplified automation model.

    The simulator follows matching triggers and predictable writes until the
    cascade settles or repeats. It is a static model, not runtime proof.
    """

    MAKS_TRIN = 400
    MAKS_DYBDE = 12

    def __init__(self, analyse: Analyse, reg: Register) -> None:
        self.a = analyse
        self.reg = reg

    def matcher(self, s: Signal, h: Hændelse, klokken: int) -> bool:
        if s.entitet != h.entitet:
            return False
        if s.art == "state":
            if s.til is not None and str(s.til) != h.tilstand:
                return False
            # `from` kan ikke afgøres uden den forrige tilstand; vi lader den
            # passere og markerer det i sporet i stedet for at gætte.
            return True
        if s.art == "numeric_state":
            try:
                v = float(h.tilstand)
            except ValueError:
                return False
            if s.over is not None and not v > s.over:
                return False
            if s.under is not None and not v < s.under:
                return False
            return True
        return False

    def kør(self, start: Hændelse, klokken: int) -> tuple[list[dict], str]:
        kø = [start]
        spor: list[dict] = []
        kørte: dict[str, int] = defaultdict(int)      # alias → antal kørsler
        set_kanter: set[tuple[str, str]] = set()
        udfald = "settled"
        trin = 0

        while kø:
            kø.sort(key=lambda h: h.tid)
            h = kø.pop(0)
            trin += 1
            if trin > self.MAKS_TRIN:
                udfald = "stopped: too many steps"
                break
            if h.dybde > self.MAKS_DYBDE:
                continue

            for x in self.a.lyttere.get(h.entitet, []):
                ramt = [s for s in x.signaler if self.matcher(s, h, klokken)]
                if not ramt:
                    continue
                if not x.vindue.overlapper(Vindue(klokken, min(1440, klokken + 1))):
                    spor.append({"art": "spring-over", "dybde": h.dybde, "auto": x.alias,
                                 "hvorfor": f"outside time window {x.vindue.beskriv()}",
                                 "via": h.entitet})
                    continue
                kørte[x.alias] += 1
                if kørte[x.alias] > 1 and x.mode == "single":
                    spor.append({"art": "droppet", "dybde": h.dybde, "auto": x.alias,
                                 "hvorfor": "mode: single, already running",
                                 "via": h.entitet})
                    continue
                if kørte[x.alias] > 6:
                    udfald = (f"potential runaway loop: {x.alias} triggered "
                              f"{kørte[x.alias]} times in the model")
                    return spor, udfald

                spor.append({"art": "vågner", "dybde": h.dybde, "auto": x.alias,
                             "via": h.entitet, "tid": h.tid, "mode": x.mode,
                             "usikker": any(s.fra is not None for s in ramt),
                             "for": max((s.forsinkelse for s in ramt), default=0.0)})

                for w in x.skrivninger:
                    ny = self._tilstand(w)
                    kant = (x.alias, w.entitet)
                    spor.append({"art": "skriver", "dybde": h.dybde + 1, "auto": x.alias,
                                 "entitet": w.entitet, "effekt": w.effekt,
                                 "service": w.service, "gren": w.gren,
                                 "tid": h.tid + w.efter_forsinkelse,
                                 "gentaget": kant in set_kanter})
                    if kant in set_kanter:
                        udfald = f"potential loop: {x.alias} writes {w.entitet} again in the model"
                        return spor, udfald
                    set_kanter.add(kant)
                    if ny is not None and self.a.lyttere.get(w.entitet):
                        kø.append(Hændelse(h.tid + w.efter_forsinkelse, w.entitet, ny,
                                           x.alias, h.dybde + 1))
        return spor, udfald

    @staticmethod
    def _tilstand(w: Skrivning) -> str | None:
        if w.effekt == "tænd":
            return "on"
        if w.effekt == "sluk":
            return "off"
        if w.effekt == "skift":
            return "on"                       # vi vælger én gren og siger det
        data = (w.rå.get("data") or {})
        for nøgle in ("value", "temperature", "brightness_pct", "option", "position"):
            if nøgle in data:
                return str(data[nøgle])
        return None                           # ingen forudsigelig ny tilstand


SIMULATION_TRACE_TYPES = {
    "vågner": "triggered",
    "skriver": "write",
    "droppet": "dropped",
    "spring-over": "skipped",
}
SIMULATION_TRACE_KEYS = {
    "art": "type",
    "dybde": "depth",
    "auto": "automation",
    "hvorfor": "reason",
    "via": "via_entity",
    "tid": "time_seconds",
    "usikker": "uncertain",
    "for": "for_seconds",
    "entitet": "entity",
    "effekt": "effect",
    "gren": "branch",
    "gentaget": "repeated",
}


def eksportér_simulationsspor(spor: list[dict]) -> list[dict]:
    """Return simulation trace entries with English public keys and values."""
    resultat = []
    for post in spor:
        offentlig = {SIMULATION_TRACE_KEYS.get(k, k): v for k, v in post.items()}
        offentlig["type"] = SIMULATION_TRACE_TYPES[post["art"]]
        if "effekt" in post:
            offentlig["effect"] = EFFECTS[post["effekt"]]
        resultat.append(offentlig)
    return resultat


def vis_simulering(spor: list[dict], udfald: str, start: Hændelse, klokken: int,
                   analyse: Analyse) -> None:
    kl_str = f"{klokken // 60:02d}:{klokken % 60:02d}"
    print(f"\n  {c.bold}if {start.entitet} becomes {start.tilstand} at {kl_str}{c.reset}")
    print(f"  {c.grey}{'─' * 74}{c.reset}\n")

    if not spor:
        lyttere = analyse.lyttere.get(start.entitet, [])
        if not lyttere:
            print(f"      {c.grey}No automation listens to {start.entitet}."
                  f" Nothing happens.{c.reset}\n")
        else:
            print(f"      {c.grey}{_antal(len(lyttere), 'automation')} listen, but none of their"
                  f" triggers match that state.{c.reset}\n")
        return

    for e in spor:
        ind = "    " + "   " * min(e["dybde"], 8)
        t = e.get("tid", 0)
        stempel = f"{c.grey}+{_tid(t):>7}{c.reset} " if t else f"{c.grey}{'':>9}{c.reset} "
        if e["art"] == "vågner":
            note = ""
            if e.get("for"):
                note += f"  {c.grey}(must remain stable for {_tid(e['for'])}){c.reset}"
            if e.get("usikker"):
                note += f"  {c.grey}(has from:, depends on the previous state){c.reset}"
            print(f"{stempel}{ind}{c.brcyan}▸ {e['auto']}{c.reset}"
                  f"{c.grey}  triggered by {e['via']} · mode {e['mode']}{c.reset}{note}")
        elif e["art"] == "skriver":
            gren = f"  {c.grey}[{' → '.join(e['gren'])}]{c.reset}" if e["gren"] else ""
            mærke = f"  {c.brred}← already written{c.reset}" if e["gentaget"] else ""
            print(f"{stempel}{ind}  {c.brgreen}{EFFECTS[e['effekt']]:<8}{c.reset} {e['entitet']}"
                  f"{c.grey}  via {e['service']}{c.reset}{gren}{mærke}")
        elif e["art"] == "droppet":
            print(f"{stempel}{ind}{c.bryellow}⊘ {e['auto']}{c.reset}"
                  f"{c.grey}  {e['hvorfor']}{c.reset}")
        elif e["art"] == "spring-over":
            print(f"{stempel}{ind}{c.grey}· {e['auto']} skipped ({e['hvorfor']}){c.reset}")

    farve = c.brred if ("loop" in udfald or "runaway" in udfald) else c.brgreen
    berørte = {e["entitet"] for e in spor if e["art"] == "skriver"}
    vakte = {e["auto"] for e in spor if e["art"] == "vågner"}
    print(f"\n  {farve}{udfald}{c.reset}"
          f"{c.grey}   ·  {_antal(len(vakte), 'automation')} triggered, "
          f"{_antal(len(berørte), 'entity', 'entities')} changed{c.reset}\n")


# ═════════════════════════════════════════════════════════════ fremvisning ══

FARVE = {"kritisk": c.brred, "advarsel": c.bryellow, "info": c.brcyan}
IKON = {"kritisk": "✕", "advarsel": "▲", "info": "●"}


def vis_fund(fund: list[Fund], filter_art: str | None, filter_alvor: str | None) -> None:
    valgt = [f for f in fund
             if (not filter_art or filter_art in f.art)
             and (not filter_alvor or f.alvor == filter_alvor)]
    if not valgt:
        print(f"\n  {c.brgreen}No findings.{c.reset}\n")
        return
    art = None
    for f in valgt:
        if f.art != art:
            art = f.art
            antal = sum(1 for x in valgt if x.art == art)
            mærkat = FINDING_KINDS[art].replace("-", " ").upper()
            print(f"\n{c.bold}  {mærkat}{c.reset}{c.grey}  ×{antal}{c.reset}")
            print(f"  {c.grey}{'─' * 74}{c.reset}")
        print(f"\n  {FARVE[f.alvor]}{IKON[f.alvor]} {f.overskrift}{c.reset}")
        for linje in f.spor:
            if linje:
                print(f"      {c.grey}{linje}{c.reset}")
    print()


CACHE_FRISK_DAGE = 3.0


def cache_alder_dage() -> float | None:
    """Return the snapshot age in days, or None when it cannot be determined."""
    hentet: float | None = None
    stempel = CACHE / ".smartbolig-refresh.json"
    if stempel.exists():
        try:
            hentet = float(json.loads(stempel.read_text(encoding="utf-8"))["completed_at"])
        except (OSError, ValueError, TypeError, KeyError):
            hentet = None
    if hentet is None:
        krav = CACHE / "device_registry.json"
        if krav.exists():
            hentet = krav.stat().st_mtime
    return None if hentet is None else (time.time() - hentet) / 86400.0


def vis_cache_alder(strøm=None) -> None:
    """Show that findings describe a snapshot rather than current runtime state."""
    strøm = sys.stdout if strøm is None else strøm
    dage = cache_alder_dage()
    if dage is None:
        print(f"  {c.bryellow}⚠ snapshot age is unknown — run"
              f" {c.brcyan}automation-lens --fetch{c.reset}", file=strøm)
    elif dage >= CACHE_FRISK_DAGE:
        print(f"  {c.bryellow}⚠ snapshot is {dage:.1f} days old — findings describe that"
              f" snapshot. Run {c.brcyan}automation-lens --fetch{c.reset}", file=strøm)
    else:
        print(f"  {c.grey}snapshot age: {dage:.1f} days{c.reset}", file=strøm)


def opsummering(fund: list[Fund], autos: list[Automation], reg: Register) -> None:
    tæl = defaultdict(int)
    for f in fund:
        tæl[f.alvor] += 1
    entiteter = {e for x in autos for e in x.rører | x.lytter_på}
    print(f"\n  {c.bold}Automation Lens{c.reset}"
          f"{c.grey}   {_antal(len(autos), 'automation')} · "
          f"{_antal(len(entiteter), 'referenced entity', 'referenced entities')} · "
          f"{_antal(len(reg.entiteter), 'known entity', 'known entities')}{c.reset}")
    dele = [f"{FARVE[a]}{IKON[a]} {tæl[a]} {SEVERITIES[a]}{c.reset}"
            for a in ("kritisk", "advarsel", "info") if tæl[a]]
    print(f"  {'  '.join(dele) if dele else c.brgreen + 'nothing to report' + c.reset}")
    vis_cache_alder()


def forklar(autos: list[Automation], analyse: Analyse, søg: str) -> None:
    træf = [x for x in autos if søg.lower() in x.alias.lower()]
    if not træf:
        print(f"\n  {c.grey}No automation name contains '{søg}'.{c.reset}\n")
        return
    for x in træf[:6]:
        print(f"\n  {c.bold}{x.alias}{c.reset}"
              f"{c.grey}  mode: {x.mode} · {x.vindue.beskriv()}{c.reset}")
        print(f"  {c.grey}{'─' * 74}{c.reset}")
        print(f"  {c.brcyan}triggered by{c.reset}")
        for s in x.signaler[:8]:
            print(f"      {s.beskriv()}")
        if not x.signaler:
            print(f"      {c.grey}(no triggers found){c.reset}")
        print(f"  {c.brgreen}writes to{c.reset}")
        efter_gren = defaultdict(list)
        for w in x.skrivninger:
            efter_gren[w.gren].append(w)
        for gren, ws in list(efter_gren.items())[:8]:
            mærkat = " → ".join(gren) if gren else "always"
            print(f"      {c.grey}{mærkat}{c.reset}")
            for w in ws[:6]:
                sen = f"  {c.grey}(after {_tid(w.efter_forsinkelse)}){c.reset}" if w.efter_forsinkelse else ""
                print(f"        {EFFECTS[w.effekt]:<8} {w.entitet}{sen}")
        naboer = set()
        for e in x.rører:
            for y in analyse.lyttere.get(e, []):
                if y is not x:
                    naboer.add((y.alias, e))
        if naboer:
            print(f"  {c.bryellow}triggers other automations{c.reset}")
            for alias, e in sorted(naboer)[:8]:
                print(f"      {alias}  {c.grey}(via {e}){c.reset}")
        rivaler = set()
        for e in x.rører:
            for y in analyse.skrivere.get(e, []):
                if y is not x:
                    rivaler.add((y.alias, e))
        if rivaler:
            print(f"  {c.brmagenta}shares entities with{c.reset}")
            for alias, e in sorted(rivaler)[:8]:
                print(f"      {alias}  {c.grey}({e}){c.reset}")
    print()


def kæde(analyse: Analyse, reg: Register, entitet: str, dybde: int = 3) -> None:
    """Show which automations write to and are triggered by one entity."""
    if not analyse.skrivere.get(entitet) and not analyse.lyttere.get(entitet):
        nær = [e for e in reg.entiteter if entitet.lower() in e.lower()][:8]
        print(f"\n  {c.grey}No automation references {entitet}.{c.reset}")
        if nær:
            print(f"  {c.grey}Did you mean: {', '.join(nær)}{c.reset}")
        print()
        return

    print(f"\n  {c.bold}{entitet}{c.reset}  {c.grey}{reg.navn(entitet)}{c.reset}")
    print(f"  {c.grey}{'─' * 74}{c.reset}")
    skrivere = analyse.skrivere.get(entitet, [])
    print(f"\n  {c.brgreen}written by {len(skrivere)}{c.reset}")
    for x in skrivere[:12]:
        eff = sorted({w.effekt for w in x.skrivninger if w.entitet == entitet})
        print(f"      {'/'.join(EFFECTS[e] for e in eff):<16} {x.alias}  {c.grey}({x.vindue.beskriv()}){c.reset}")

    lyttere = analyse.lyttere.get(entitet, [])
    print(f"\n  {c.brcyan}triggers {_antal(len(lyttere), 'automation')}{c.reset}")
    for x in lyttere[:12]:
        print(f"      {x.alias}")

    if lyttere:
        print(f"\n  {c.bryellow}then{c.reset}")
        set_ = {entitet}
        lag = lyttere
        for d in range(1, dybde):
            næste, linjer = [], []
            for x in lag:
                for e in sorted(x.rører):
                    if e in set_:
                        continue
                    set_.add(e)
                    linjer.append(f"{'  ' * d}└─ {x.alias} {c.grey}writes{c.reset} {e}")
                    næste += analyse.lyttere.get(e, [])
            for l in linjer[:10]:
                print(f"      {l}")
            if not næste:
                break
            lag = næste
    print()


# ══════════════════════════════════════════════════════════════════ hent ══

def ha_token(env=None) -> str | None:
    """Return the first non-empty supported Home Assistant token variable."""
    env = os.environ if env is None else env
    for navn in HA_TOKEN_VARS:
        værdi = (env.get(navn) or "").strip()
        if værdi:
            return værdi
    return None


# ── minimal WebSocket-klient (RFC 6455, kun det HA's API kræver) ──────────

def ws_frame(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    """Build one masked client frame using a four-byte random mask by default."""
    mask = os.urandom(4) if mask is None else mask
    hoved = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        hoved.append(0x80 | n)
    elif n < 65536:
        hoved.append(0x80 | 126)
        hoved += struct.pack(">H", n)
    else:
        hoved.append(0x80 | 127)
        hoved += struct.pack(">Q", n)
    hoved += mask
    return bytes(hoved) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def ws_parse(buf: bytes) -> tuple[bool, int, bytes, int] | None:
    """Parse the first complete frame as (fin, opcode, payload, bytes consumed)."""
    if len(buf) < 2:
        return None
    fin, opcode = bool(buf[0] & 0x80), buf[0] & 0x0F
    masked, n = bool(buf[1] & 0x80), buf[1] & 0x7F
    off = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, off = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, off = struct.unpack(">Q", buf[2:10])[0], 10
    nøgle = b""
    if masked:
        if len(buf) < off + 4:
            return None
        nøgle, off = buf[off:off + 4], off + 4
    if len(buf) < off + n:
        return None
    payload = buf[off:off + n]
    if masked:
        payload = bytes(b ^ nøgle[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, off + n


class WebSocket:
    """Exchange text messages over one Home Assistant WebSocket connection."""

    def __init__(self, sock, rest: bytes = b"") -> None:
        self.sock = sock
        self.buf = bytearray(rest)

    @classmethod
    def forbind(cls, url: str, sti: str = "/api/websocket", timeout: float = 60.0) -> "WebSocket":
        u = urlsplit(url)
        vært = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        sock = socket.create_connection((vært, port), timeout=timeout)
        if u.scheme == "https":
            import ssl
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=vært)
        nøgle = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (f"GET {sti} HTTP/1.1\r\nHost: {vært}:{port}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {nøgle}\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        svar = b""
        while b"\r\n\r\n" not in svar:
            bid = sock.recv(4096)
            if not bid:
                raise OSError("WebSocket handshake was interrupted")
            svar += bid
        hoved, rest = svar.split(b"\r\n\r\n", 1)
        status_line = hoved.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise OSError(f"WebSocket handshake was rejected: {status_line!r}")
        return cls(sock, rest)

    def send_tekst(self, tekst: str) -> None:
        self.sock.sendall(ws_frame(0x1, tekst.encode("utf-8")))

    def modtag_tekst(self) -> str:
        besked = bytearray()
        while True:
            ramme = ws_parse(bytes(self.buf))
            if ramme is None:
                bid = self.sock.recv(1 << 16)
                if not bid:
                    raise OSError("WebSocket connection was closed")
                self.buf += bid
                continue
            fin, opcode, payload, brugt = ramme
            del self.buf[:brugt]
            if opcode == 0x9:                       # ping → pong
                self.sock.sendall(ws_frame(0xA, payload))
            elif opcode == 0x8:
                raise OSError("Home Assistant closed the WebSocket connection")
            elif opcode in (0x0, 0x1):
                besked += payload
                if fin:
                    return besked.decode("utf-8")

    def luk(self) -> None:
        try:
            self.sock.sendall(ws_frame(0x8, b""))
        except OSError:
            pass
        self.sock.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward a Home Assistant bearer token to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


class HaApi:
    """Read-only client for Home Assistant REST and WebSocket APIs."""

    def __init__(self, url: str, token: str, timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, sti: str):
        req = urllib.request.Request(self.url + sti, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as svar:
            return json.loads(svar.read().decode("utf-8"))

    def ws_list(self, *typer: str) -> dict[str, list]:
        """Run multiple list commands in one WebSocket session; return {type: result}."""
        ws = WebSocket.forbind(self.url, timeout=self.timeout)
        try:
            hilsen = json.loads(ws.modtag_tekst())
            if hilsen.get("type") != "auth_required":
                raise OSError(f"unexpected Home Assistant greeting: {hilsen.get('type')!r}")
            ws.send_tekst(json.dumps({"type": "auth", "access_token": self.token}))
            auth = json.loads(ws.modtag_tekst())
            if auth.get("type") != "auth_ok":
                raise OSError("Home Assistant rejected the token (auth_invalid)")
            ud: dict[str, list] = {}
            for nr, art in enumerate(typer, start=1):
                ws.send_tekst(json.dumps({"id": nr, "type": art}))
                while True:
                    svar = json.loads(ws.modtag_tekst())
                    if svar.get("id") == nr and svar.get("type") == "result":
                        break
                if not svar.get("success"):
                    raise OSError(f"{art}: {svar.get('error', {}).get('message', 'unknown error')}")
                if not isinstance(svar.get("result"), list):
                    raise OSError(f"{art}: result is not a list")
                ud[art] = svar["result"]
            return ud
        finally:
            ws.luk()


def _skriv_atomisk(mål: Path, data: bytes) -> None:
    mål.parent.mkdir(parents=True, exist_ok=True)
    midlertidig = mål.with_name(f".{mål.name}.{os.getpid()}.tmp")
    try:
        midlertidig.write_bytes(data)
        os.replace(midlertidig, mål)
    finally:
        midlertidig.unlink(missing_ok=True)


def _registerfil(nøgle: str, felt: str, poster: list) -> bytes:
    """Serialize registry data in the subset of Home Assistant storage format we read."""
    return json.dumps({"version": 1, "minor_version": 0, "key": nøgle, "data": {felt: poster}},
                      ensure_ascii=False).encode("utf-8")


def hent_via_api(api, cache: Path | None = None, skriv=print) -> int:
    """Fetch registries and automation configuration into a local snapshot.

    All sources are fetched before any files are replaced, so an interrupted
    fetch cannot leave a partial snapshot. ``api`` must provide ``get`` and
    ``ws_list`` methods.
    """
    cache = CACHE if cache is None else cache
    registre = api.ws_list("config/device_registry/list", "config/entity_registry/list")
    enheder = registre["config/device_registry/list"]
    entiteter = registre["config/entity_registry/list"]

    tilstande = api.get("/api/states")
    automationer: list[dict] = []
    scripts: dict[str, dict] = {}
    sprunget_over = 0
    for tilstand in tilstande:
        eid = tilstand.get("entity_id", "")
        if eid.startswith("automation."):
            aid = (tilstand.get("attributes") or {}).get("id")
            if not aid:
                continue                            # YAML-pakke uden id: kan ikke slås op
            try:
                konfig = api.get(f"/api/config/automation/config/{aid}")
            except urllib.error.HTTPError as fejl:
                if fejl.code == 404:
                    sprunget_over += 1              # står i en YAML-pakke, ikke i automations.yaml
                    continue
                raise
            if isinstance(konfig, dict):
                konfig.setdefault("id", str(aid))
                automationer.append(konfig)
        elif eid.startswith("script."):
            oid = eid.split(".", 1)[1]
            try:
                konfig = api.get(f"/api/config/script/config/{oid}")
            except urllib.error.HTTPError as fejl:
                if fejl.code == 404:
                    continue                        # YAML-pakke: findes ikke i editoren
                raise
            if isinstance(konfig, dict):
                scripts[oid] = konfig

    if not (enheder and entiteter and automationer):
        print("  Home Assistant responded but returned no devices, entities or automations.", file=sys.stderr)
        return 1

    filer = {
        "automations.yaml": yaml.safe_dump(automationer, allow_unicode=True, sort_keys=False).encode("utf-8"),
        "scripts.yaml": yaml.safe_dump(scripts, allow_unicode=True, sort_keys=False).encode("utf-8"),
        "device_registry.json": _registerfil("core.device_registry", "devices", enheder),
        "entity_registry.json": _registerfil("core.entity_registry", "entities", entiteter),
    }
    for navn, data in filer.items():
        _skriv_atomisk(cache / navn, data)
        skriv(f"  {c.brgreen}✓{c.reset} {navn}  {c.grey}{len(data) / 1024:.0f} KB{c.reset}")
    skriv(f"  {c.grey}{_antal(len(automationer), 'automation')} · "
          f"{_antal(len(scripts), 'script')} · "
          f"{_antal(len(entiteter), 'entity', 'entities')} · "
          f"{_antal(len(enheder), 'device')}"
          + (f" · skipped {_antal(sprunget_over, 'YAML automation')}"
             " without editor configuration"
             if sprunget_over else "") + c.reset)
    return 0


def hent() -> int:
    """Fetch only from an explicitly configured Home Assistant API."""
    try:
        url = urlsplit(HA_URL)
        valid = (url.scheme in ("http", "https") and url.hostname
                 and not url.username and not url.password
                 and not url.query and not url.fragment)
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        print("Set KS_HA_URL to your Home Assistant http(s) URL without credentials, query or fragment.", file=sys.stderr)
        return 1
    token = ha_token()
    if not token:
        print("Set KS_HA_TOKEN to a Home Assistant long-lived access token before --fetch.", file=sys.stderr)
        return 1
    try:
        return hent_via_api(HaApi(HA_URL, token))
    except urllib.error.HTTPError as fejl:
        print(f"  Home Assistant returned HTTP {fejl.code} (redirects are refused).", file=sys.stderr)
    except (urllib.error.URLError, OSError, ValueError) as fejl:
        print(f"  Fetch from Home Assistant failed: {fejl}", file=sys.stderr)
    return 1


def indlæs_alt() -> tuple[list[Automation], Register]:
    krav = CACHE / "device_registry.json"
    if not krav.exists():
        print(f"No snapshot found. Run: {c.brcyan}automation-lens --fetch{c.reset}", file=sys.stderr)
        sys.exit(1)
    dev = json.loads((CACHE / "device_registry.json").read_text(encoding="utf-8"))
    ent = json.loads((CACHE / "entity_registry.json").read_text(encoding="utf-8"))
    reg = Register(dev["data"]["devices"], ent["data"]["entities"])
    autos = []
    for navn, mærkat in (("automations.yaml", "automation"), ("scripts.yaml", "script")):
        f = CACHE / navn
        if f.exists():
            autos += indlæs(reg, f, mærkat)
    return autos, reg


def main() -> int:
    global CACHE, HA_URL
    ap = argparse.ArgumentParser(
        prog="automation-lens",
        description="Automation Lens: read-only static analysis of Home Assistant automations.",
        epilog='Try: automation-lens --data-dir examples/demo --findings')
    ap.add_argument("event", nargs="*",
                    help='event to simulate, e.g. "light.demo_lamp on"')
    ap.add_argument("--time", "--kl", dest="kl", metavar="HH:MM", default="12:00", help="time of day")
    ap.add_argument("--fetch", "--hent", dest="hent", action="store_true", help="fetch configuration using KS_HA_URL and KS_HA_TOKEN")
    ap.add_argument("--findings", "--fund", dest="fund", action="store_true", help="run static analyses")
    ap.add_argument("--explain", "--forklar", dest="forklar", metavar="SEARCH", help="explain matching automations")
    ap.add_argument("--chain", "--kæde", "--kaede", dest="kæde", metavar="ENTITY", help="trace references around an entity")
    ap.add_argument("--kind", "--art", dest="art", metavar="KIND", help="filter by finding kind (English or legacy Danish value)")
    ap.add_argument("--severity", "--alvor", dest="alvor", metavar="LEVEL",
                    choices=["critical", "warning", "info", "kritisk", "advarsel"],
                    help="filter by severity (English or legacy Danish value)")
    ap.add_argument("--json", action="store_true", help="JSON output with English keys and values")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("KS_DATA_DIR", "cache")), help="data directory (default: KS_DATA_DIR or ./cache)")
    ap.add_argument("--ha-url", help="Home Assistant base URL (token stays in environment)")
    args = ap.parse_args()
    CACHE = args.data_dir.expanduser().resolve()
    HA_URL = (args.ha_url or os.environ.get("KS_HA_URL") or os.environ.get("HOMEASSISTANT_URL") or os.environ.get("HASS_SERVER") or "").rstrip("/")
    args.alvor = {"critical": "kritisk", "warning": "advarsel"}.get(args.alvor, args.alvor)
    args.art = intern_art(args.art)

    if args.hent:
        return hent()

    if not (args.fund or args.forklar or args.kæde or args.event):
        ap.print_help()
        return 0
    required = ("device_registry.json", "entity_registry.json", "automations.yaml")
    missing = [name for name in required if not (CACHE / name).is_file()]
    if missing:
        print(f"Missing data: {', '.join(missing)}. Use --data-dir examples/demo, or configure your connection and run --fetch.", file=sys.stderr)
        return 1
    try:
        autos, reg = indlæs_alt()
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"Could not read configuration ({type(error).__name__}). Check the data format in docs/GUIDE.md.", file=sys.stderr)
        return 1
    a = Analyse(autos, reg)

    if args.kæde:
        kæde(a, reg, args.kæde)
        return 0
    if args.forklar:
        forklar(autos, a, args.forklar)
        return 0

    if args.event:
        dele = " ".join(args.event).replace("=", " ").replace("→", " ").split()
        entitet = dele[0]
        tilstand = dele[1] if len(dele) > 1 else "on"
        if "." not in entitet:
            nær = [e for e in reg.entiteter if entitet.lower() in e.lower()][:8]
            print(f"  '{entitet}' does not look like an entity_id."
                  + (f" Did you mean: {', '.join(nær)}" if nær else ""), file=sys.stderr)
            return 1
        klokken = tid_til_min(args.kl)
        if klokken is None:
            print(f"  Could not parse time '{args.kl}'.", file=sys.stderr)
            return 1
        start = Hændelse(0.0, entitet, tilstand, "dig", 0)
        spor, udfald = Simulator(a, reg).kør(start, klokken)
        if args.json:
            print(json.dumps({"start": {"entity": entitet, "state": tilstand,
                                        "time": args.kl},
                              "outcome": udfald, "trace": eksportér_simulationsspor(spor)},
                             ensure_ascii=False, indent=1, default=str))
            return 0
        vis_simulering(spor, udfald, start, klokken, a)
        return 0

    if not args.fund:
        ap.print_help()
        print(f"\n  {c.grey}No event was provided. Try --findings for static"
              f" analysis, or provide an event:{c.reset}")
        print(f'  {c.brcyan}automation-lens "light.demo_lamp on" --time 23:30{c.reset}\n')
        return 0

    fund = [f for f in a.alle()
            if (not args.art or args.art in f.art)
            and (not args.alvor or args.alvor == f.alvor)]
    if args.json:
        vis_cache_alder(sys.stderr)
        print(json.dumps([eksportér_fund(f) for f in fund],
            ensure_ascii=False, indent=1))
        return 0

    opsummering(fund, autos, reg)
    vis_fund(fund, args.art, args.alvor)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
