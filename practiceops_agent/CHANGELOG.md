# Changelog

## 2.3.0

- Sends every UniFi user's card IDs every 5 minutes, so the PracticeOps Employees list shows who really
  has a badge (badges can now be added, replaced and removed right from that list).
- Sends a read-only snapshot of Home Assistant (version, devices and sensors; never people or places)
  for the new Home Assistant page in PracticeOps.

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
