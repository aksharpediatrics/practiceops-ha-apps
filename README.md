# PracticeOps Home Assistant Apps

Home Assistant Apps (add-ons) for Akshar Pediatrics' PracticeOps.

| App | What it does |
|---|---|
| [PracticeOps Office Agent](practiceops_agent/DOCS.md) | Carries out PracticeOps UniFi user and card requests and sends the IT / Network dashboard, in office hours |

## Install

In Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**, add
`https://github.com/aksharpediatrics/practiceops-ha-apps`, then install **PracticeOps Office Agent** and
follow its Documentation tab.

This repository holds code only. Tokens and keys are entered in Home Assistant and never stored here.

## Vaccine fridge and freezer monitor (ESPHome)

[`esphome/vaccine-temps.yaml`](esphome/vaccine-temps.yaml) is an ESP32 with two FS200-SHT10 probes.
ESPHome has no SHT10 support built in, so [`components/sht1x`](components/sht1x) adds it; the device
file loads it from a fixed tag (`sht1x-1.0.0`), so later changes here never reach the device by
surprise. Alarms run in Home Assistant, not in PracticeOps.

## Development

`practiceops_agent/agent.py` uses only the Python standard library. Run the tests with
`python -m pytest practiceops_agent/tests`. When you change the agent, bump `version` in
`practiceops_agent/config.yaml` and add a line to `practiceops_agent/CHANGELOG.md`: Home Assistant
only offers an update when the version changes.
