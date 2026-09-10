# Automation Lens JSON reference

[README](../README.md) · [User guide](GUIDE.md) · [Analysis boundaries](ANALYSIS.md)

Automation Lens writes JSON to standard output when `--json` is used. Snapshot-age notices go to standard error, so they do not corrupt redirected JSON.

## Findings

```shell
automation-lens --data-dir examples/demo --findings --json > findings.json
```

The result is an array. Each finding has this shape:

```json
{
  "kind": "feedback-loop",
  "severity": "warning",
  "title": "Potential feedback loop between 2 automations (all use single mode, which may limit repeated runs)",
  "trace": [
    "Demo - relay follows lamp --[switch.demo_relay]--> Demo - relay switches lamp off"
  ],
  "automations": ["Demo - relay follows lamp", "Demo - relay switches lamp off"]
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | string | Stable machine-readable finding category. |
| `severity` | string | `critical`, `warning` or `info`. |
| `title` | string | Short English explanation for people. |
| `trace` | array of strings | Evidence from the simplified static model. |
| `automations` | array of strings | Automation aliases associated with the finding. |

The supported `kind` values are:

| Kind | What it identifies |
| --- | --- |
| `feedback-loop` | A cycle in the graph of automation writes and triggers. Runtime conditions still determine whether it repeats. |
| `self-trigger` | An automation observes and writes the same entity. |
| `conflicting-write` | Two automations may write opposing states to one entity. |
| `missing-reference` | A referenced item is absent from the analyzed registry snapshot. |
| `delay-trap` | A long-running `single` automation may drop later triggers. |
| `impossible-condition` | Top-level conditions cannot be satisfied together. |
| `overlapping-automation` | Automations share a trigger signature and written entities. |

Severity indicates review priority within the model:

| Severity | Meaning |
| --- | --- |
| `critical` | The modeled configuration has a strong contradiction or a loop pattern whose mode permits repeated runs. |
| `warning` | The pattern can affect behavior and should be checked against conditions and Home Assistant traces. |
| `info` | A relationship or overlap is worth understanding but may be intentional. |

Use `--kind feedback-loop` or `--severity warning` before `--json` to filter the array. Legacy Danish kind and severity values remain accepted as input, but exported values are always English. A finding does not change the exit status: exit 0 means analysis completed, not that the array is empty.

## Simulation

```shell
automation-lens --data-dir examples/demo "light.demo_lamp on" --time 23:30 --json
```

The result is one object:

```json
{
  "start": {
    "entity": "light.demo_lamp",
    "state": "on",
    "time": "23:30"
  },
  "outcome": "settled",
  "trace": []
}
```

`outcome` is an English diagnostic message rather than an enum. Consumers should use the structured trace instead of matching its text. Trace entries use one of these `type` values:

| Type | Fields in addition to `type` |
| --- | --- |
| `triggered` | `depth`, `automation`, `via_entity`, `time_seconds`, `mode`, `uncertain`, `for_seconds` |
| `write` | `depth`, `automation`, `entity`, `effect`, `service`, `branch`, `time_seconds`, `repeated` |
| `dropped` | `depth`, `automation`, `reason`, `via_entity` |
| `skipped` | `depth`, `automation`, `reason`, `via_entity` |

`depth` is the modeled cascade depth. `time_seconds` is relative to the input event. `branch` is an array of branch labels. Write effects are `turn on`, `turn off`, `toggle` or `set`.

The simulator does not call Home Assistant and does not reproduce templates or runtime state. Treat its output as a path through the exported model, not a live trace.

## Handling output safely

Entity IDs, automation aliases and traces can reveal details about a home. Store reports with the same care as the source snapshot, and remove private identifiers before attaching JSON to a public issue.
