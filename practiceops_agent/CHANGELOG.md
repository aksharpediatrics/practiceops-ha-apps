# Changelog

## 2.2.0

- Sends the vaccine fridge and freezer temperatures from Home Assistant to PracticeOps (5-minute
  averages; overnight and weekends in one batch when the office opens). No new token needed.

## 2.1.0

- The daily UniFi to PracticeOps user sync (badges made in UniFi are linked to employees by exact
  name), replacing the Windows "PracticeOps UniFi User Sync" task.

## 2.0.0

- First Home Assistant version, replacing the Windows scheduled task.
- UniFi requests start within a couple of seconds, instead of up to a minute.
- A daily check that everyone removed from PracticeOps is deactivated in UniFi.
