# Automation Lens — dansk brugervejledning

[README](../README.md) · [English guide](GUIDE.md) · [Analysegrænser](ANALYSIS.md)

Automation Lens er et skrivebeskyttet kommandolinjeværktøj til at undersøge Home Assistant-automatiseringer. Det kobler enheder, entiteter, triggere, betingelser og handlinger sammen og peger på mulige kredsløb, modstridende handlinger, manglende referencer, lange forsinkelser og overlappende regler.

Et fund er et sted, du bør undersøge. Det er ikke bevis for en fejl i dit kørende hjem. Værktøjet udfører aldrig Home Assistant-handlinger og ændrer ikke din konfiguration.

## 1. Installér i et virtuelt Python-miljø

Du skal bruge Python 3.10 eller nyere. Download projektet fra GitHub, eller klon det, og åbn PowerShell i mappen med `pyproject.toml`:

```powershell
git clone https://github.com/Hovborg/automation-lens.git
Set-Location .\automation-lens
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\automation-lens.exe --help
```

Der kræves ingen administratorrettigheder, og det virtuelle miljø behøver ikke blive aktiveret. Brug den fulde sti `.\.venv\Scripts\automation-lens.exe` i de følgende eksempler. Hvis du aktiverer miljøet, kan du bruge `automation-lens` direkte.

Behold projektmappen. Demoen og vejledningerne ligger i den og installeres ikke nødvendigvis sammen med Python-pakken.

## 2. Lær værktøjet at kende med den fiktive demo

Kør først den medfølgende offline-demo:

```powershell
.\.venv\Scripts\automation-lens.exe --data-dir examples\demo --findings
```

Demoen bruger kun opdigtede enheder og fem bevidst problematiske automatiseringer:

| Eksempel | Hvad fundet betyder |
| --- | --- |
| En lampe styrer et relæ, og relæet styrer lampen | Grafen indeholder et kredsløb. De præcise tilstandsbetingelser kan stadig stoppe det, så fundet beviser ikke en uendelig løkke. |
| Bevægelse tænder lampen, mens en anden regel slukker den senere | To regler skriver modsatte tilstande. Den forsinkede slukning kan være tilsigtet. |
| En udgået lampe er stadig nævnt | Referencen kunne ikke findes i demoens registersnapshot. |
| Fem minutters forsinkelse i `single`-tilstand | Nye triggere kan blive ignoreret, mens automatiseringen allerede kører. |
| To regler deler trigger og mål | Reglerne overlapper muligvis, men kan stadig have forskellige betingelser eller tilsigtet timing. |

Demoens YAML er kun testdata. Kopiér den ikke ind i et rigtigt Home Assistant-system.

## 3. Følg en entitet, forklar en regel og simulér en hændelse

```powershell
.\.venv\Scripts\automation-lens.exe --data-dir examples\demo --chain light.demo_lamp
.\.venv\Scripts\automation-lens.exe --data-dir examples\demo --explain "relay"
.\.venv\Scripts\automation-lens.exe --data-dir examples\demo "light.demo_lamp on" --time 23:30
```

- `--chain` viser relationerne omkring en entitet.
- `--explain` søger i automatiseringsnavne og relaterede referencer.
- En positionshændelse som `"light.demo_lamp on"` starter den forenklede simulator.
- `--time` bruger 24-timers klokkeslæt og gør tidsbetingelser i modellen tydeligere.

Simulationen sender ingen hændelse til Home Assistant. Den viser en mulig kæde ud fra de eksporterede filer og den forenklede model; den er ikke et live-spor fra Home Assistants trace-visning.

## 4. Brug din egen Home Assistant-konfiguration

Opret en særskilt datamappe **uden for Git-repositoryet**. Standardmappen er `cache` under den aktuelle arbejdsmappe. `--data-dir` har forrang for miljøvariablen `KS_DATA_DIR`, som igen har forrang for standardmappen.

En hentning erstatter de fire datafiler i den valgte mappe. Brug derfor aldrig en mappe med originale Home Assistant-filer eller andre filer, du ikke vil have overskrevet.

### Mulighed A: hent skrivebeskyttet gennem API'et

Opret et langtidstoken på din Home Assistant-brugerprofil. Tokenet arver brugerens almindelige rettigheder; det er ikke teknisk begrænset til læsning, selv om Automation Lens kun foretager læsekald. Nogle konfigurationsendpoints kan kræve en administratorkonto.

PowerShell 7 kan læse tokenet skjult, så det ikke skrives direkte i kommandohistorikken:

```powershell
$env:KS_HA_URL = 'https://your-home-assistant.example'
$env:KS_HA_TOKEN = Read-Host 'Home Assistant-token' -MaskInput
.\.venv\Scripts\automation-lens.exe --data-dir "$HOME\automation-lens-data" --fetch
Remove-Item Env:\KS_HA_TOKEN
.\.venv\Scripts\automation-lens.exe --data-dir "$HOME\automation-lens-data" --findings
```

Erstat eksempeladressen med Home Assistants direkte, endelige basisadresse. Redirects afvises, så et Authorization-header ikke bliver sendt videre til en anden placering. HTTPS-certifikater kontrolleres. Almindelig HTTP kan bruges på et betroet lokalt netværk, men krypterer ikke tokenet under transport.

Følgende miljøvariabler understøttes:

| Formål | Førstevalg | Alternativer |
| --- | --- | --- |
| Adresse | `KS_HA_URL` | `HOMEASSISTANT_URL`, `HASS_SERVER` |
| Token | `KS_HA_TOKEN` | `HOMEASSISTANT_TOKEN`, `HASS_TOKEN` |

`--ha-url` kan tilsidesætte adressen. Der findes med vilje ingen kommandolinjeparameter til tokenet. Læg aldrig et token i URL'en, en kommandolinje, en fil i repositoryet eller en offentlig fejlrapport.

Hentningen læser enheds- og entitetsregistrene via WebSocket, bruger entitetstilstande til at finde automatiseringer og scripts og henter deres editor-konfiguration via REST. Automatiseringer i YAML-pakker uden en editor-konfiguration kan blive udeladt. En vellykket hentning beviser derfor ikke fuld dækning. Værktøjet forsøger ikke SSH-adgang.

### Mulighed B: analysér allerede eksporterede filer

Læg eksporterede kopier i en dedikeret mappe:

| Fil | Forventet indhold |
| --- | --- |
| `device_registry.json` | Et objekt med `data.devices`, som er en liste af enheder. |
| `entity_registry.json` | Et objekt med `data.entities`, som er en liste af entiteter. |
| `automations.yaml` | En liste af Home Assistant-automatiseringer. |
| `scripts.yaml` | Valgfrit objekt, hvor hvert script har en `sequence`. |

Mappen `examples/demo` viser de mindste gyldige former. Registry-snapshots følger samme ydre struktur som Home Assistants registerfiler. Arbejd altid på kopier; redigér aldrig livefiler i `.storage`.

Hvis konfigurationen er fordelt på pakker, skal du selv fremstille en samlet analysekopi. Egne YAML-tags, `!include`, templates, variable og integrationsspecifik adfærd udvides ikke fuldstændigt.

## 5. Filtrér og gem resultater

Vis kun fund fra et bestemt alvorlighedsniveau:

```powershell
.\.venv\Scripts\automation-lens.exe --data-dir "$HOME\automation-lens-data" --findings --severity warning
```

Gem maskinlæsbar JSON:

```powershell
.\.venv\Scripts\automation-lens.exe --data-dir "$HOME\automation-lens-data" --findings --json > findings.json
```

JSON skrives til standard-output. Beskeder om snapshot-alder skrives til standard-error, så de ikke blandes ind i JSON-filen. Et alvorlighedsfilter gælder for både tekst og JSON.

Returkoder:

| Kode | Betydning |
| --- | --- |
| `0` | Den ønskede analyse blev gennemført. Der kan stadig være fund. |
| `1` | Fejl i input, forbindelse eller kørsel. |
| `2` | Ugyldige kommandolinjeargumenter. |

Returkode `0` er ikke en garanti for, at konfigurationen er fejlfri.

## 6. Fortolk fundene forsigtigt

Automation Lens er en statisk analyse og en lille simulator. Den kender ikke hele Home Assistants runtime:

- Et grafkredsløb kan stoppe med det samme, fordi en præcis tilstandsbetingelse ikke passer.
- `choose`, `if`, templates, variable, integrationer og den aktuelle enhedstilstand modelleres ikke fuldstændigt.
- Forsinkede modsatrettede handlinger er ofte tilsigtede.
- En manglende entitet kan være en helper eller dynamisk entitet, som ikke findes i snapshot'et.
- Automatiseringer fra YAML-pakker kan mangle i API-eksporten.
- Filernes tidsstempler viser snapshot-alder, ikke enhedernes aktuelle tilstand.
- En hentning skriver hver fil atomisk, men alle fire filer udskiftes ikke i én samlet transaktion. Kør hentningen igen efter afbrydelse eller diskfejl.

Brug dette kontrolforløb for hvert vigtigt fund:

1. Læs den viste kæde og de nævnte automatiseringer.
2. Find de samme automatiseringer i Home Assistant.
3. Sammenlign præcise triggere, betingelser, mål og tilstandsværdier.
4. Undersøg Home Assistants egne traces og historik.
5. Afgør først derefter, om adfærden er forkert eller tilsigtet.

Se også [analysegrænserne](ANALYSIS.md).

## 7. Fejlfinding

| Problem | Næste kontrol |
| --- | --- |
| `python` findes ikke | Installér Python fra [python.org](https://www.python.org/downloads/), åbn PowerShell igen, og kør `python --version`. |
| Kommandoen `automation-lens` findes ikke | Brug den fulde sti `.\.venv\Scripts\automation-lens.exe`. |
| Datafiler mangler | Kontrollér `--data-dir`; prøv derefter demoen, eller hent igen til en dedikeret mappe. |
| YAML eller JSON er ugyldig | Sammenlign indrykning og filform med `examples/demo`. Includes bliver ikke udvidet. |
| HTTP 401 eller 403 | Kontrollér token, URL og brugerens rettigheder. Tilbagekald og opret tokenet igen, hvis det kan være blevet vist eller gemt. |
| Redirect afvist | Brug den direkte endelige Home Assistant-adresse, og kontrollér reverse proxy-konfigurationen. |
| TLS- eller netværksfejl | Kontrollér certifikat og adgang fra den computer, hvor kommandoen kører. Deaktivér ikke certifikatkontrollen. |
| Et fund virker forkert | Kontrollér den konkrete automation og dens trace, og sammenlign med modellens grænser. |

## 8. Opdatér eller fjern værktøjet

Opdatér en Git-checkout ved at hente den ønskede revision og geninstallere pakken med Python fra det virtuelle miljø:

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\automation-lens.exe --data-dir examples\demo --findings
```

Bevar først en kopi af eventuelle lokale kodeændringer. Kør demoen igen, før du analyserer egne eksporter.

Afinstallér Python-pakken:

```powershell
.\.venv\Scripts\python.exe -m pip uninstall automation-lens
```

Hvis miljøet kun blev brugt til Automation Lens, kan du i stedet lukke processer, der bruger det, og slette `.venv`-mappen. Eksporterede data ligger uafhængigt i den mappe, du valgte, og fjernes ikke automatisk.

Automation Lens opretter ingen tjeneste, planlagt opgave eller Home Assistant-automation.

## 9. Del en sikker fejlrapport

Del aldrig en rigtig Home Assistant-eksport offentligt. Registry-filer, automatiseringer, JSON-rapporter og logs kan afsløre navne, adresser, lokationer, familiemønstre, webhook-id'er og andre private oplysninger.

Lav i stedet et minimalt syntetisk eksempel:

1. Erstat alle entiteter med opdigtede navne som `light.demo_lamp` og `binary_sensor.demo_motion`.
2. Fjern personer, adresser, GPS-data, enheds-id'er, webhook-id'er, tokens og hemmeligheder.
3. Behold kun de få triggere, betingelser og handlinger, der er nødvendige for at vise problemet.
4. Kontrollér, at eksemplet stadig reproducerer fundet med `--data-dir`.
5. Indsæt kun det syntetiske eksempel og kommandoens ufølsomme output i GitHub-issuen.

Hvis et token kan være blevet delt, skal det tilbagekaldes i Home Assistant med det samme.
