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

## Development

`practiceops_agent/agent.py` uses only the Python standard library. Run the tests with
`python -m pytest practiceops_agent/tests`. When you change the agent, bump `version` in
`practiceops_agent/config.yaml` and add a line to `practiceops_agent/CHANGELOG.md`: Home Assistant
only offers an update when the version changes.
