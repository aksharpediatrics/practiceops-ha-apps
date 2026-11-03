# PracticeOps Office Agent

PracticeOps runs in the cloud and can't reach the UniFi console on the office network. This App does the
office side:

- **UniFi users and cards.** Requests made on an employee's PracticeOps profile (create the UniFi user,
  enroll a card, remove a card, deactivate) start within a couple of seconds. Once a day it also checks
  that everyone removed from PracticeOps is deactivated in UniFi, and sends PracticeOps the names of
  active UniFi users so badges made in UniFi are linked to the right employee for the time clock.
- **IT / Network dashboard.** It reads the console every 5 minutes, and the live throughput every
  minute.

Door taps never go through this App. The readers and the console decide those on their own, so doors
keep working if the Pi, PracticeOps or the internet is down.

It works in office hours only (Mon–Fri 7:45–5:15, Sat 7:45–12:15) so PracticeOps can sleep at night.
Requests made at other times wait for the next morning.

## Setup

1. In PracticeOps, open **Admin → Network** and click **Create token**. Copy it.
2. Here, open the **Configuration** tab and paste:
   - **PracticeOps token**: the token you just copied.
   - **UniFi Access API key** and **UniFi Network API key**: the two keys you created in UniFi. The
     Access key needs permission to edit users and credentials (cards) and to view devices and access
     policies.
3. Click **Save**, then go to the **Info** tab and click **Start**. Turn on **Start on boot** and
   **Watchdog**.
4. The **Log** tab should say `started`. Within a minute, PracticeOps → Admin → Network shows
   **Connected**.

## Privacy

Before anything leaves the office, Wi-Fi passwords and keys are removed and device hardware (MAC)
addresses are replaced with codes. A card's secret token never leaves the office, only its printed
ID. The keys stay on the Pi. They are included in Home Assistant backups, which are encrypted.
