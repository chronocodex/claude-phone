# Asterisk / FreePBX Setup

This assumes you already have a working FreePBX or plain Asterisk install
with extensions registered (a Yealink, a softphone like Linphone, etc.).
This guide does **not** cover installing FreePBX itself — there are good
existing guides for that; link one from your own README if you're
following along without one.

**Requirement:** your Asterisk build needs the `AudioSocket` application.
It's included by default in modern Asterisk (18+) and FreePBX 16+. Confirm
with `asterisk -rx "core show application AudioSocket"` — if that errors,
you'll need to update Asterisk first.

## Tier 1 — Inbound (dial in and talk)

1. Add the `[claude-phone-custom]` context from
   `extensions_custom.conf.example` to your `extensions_custom.conf`
   (FreePBX: **Admin → Config Edit**, or edit the file directly if you have
   shell access). Replace `YOUR_SERVER_IP` with the machine running
   `server.py`, and generate your own UUID for the placeholder.

2. Create a **Custom Destination** (FreePBX: **Admin → Custom
   Destinations**) pointing at `claude-phone-custom,s,1`.

3. Create a **feature code** to actually dial it — FreePBX: **Admin →
   Feature Codes**, or a **Misc Application** (**Admin → Applications →
   Misc Application**) mapped to a code like `*99`, pointing at the Custom
   Destination from step 2.

4. **Apply Config**, then dial your feature code from a registered
   extension. You should hear the greeting.

That's the entire Tier 1 setup — no AMI, no monitoring config needed for
this part to work.

## Tier 3 — Outbound alerting (optional)

Only needed if you're enabling alerting (`ami.host` set in
`config.yaml`).

1. Add the `[claude-phone-alert]` context the same way as step 1 above,
   with a **different** UUID than the inbound one.

2. **Create an AMI user** — FreePBX: **Settings → Asterisk Manager Users**
   (or edit `/etc/asterisk/manager.conf` directly under a Custom
   Destination if you're not on FreePBX). Give it:
   - A username matching `ami.user` in `config.yaml` (default `claudephone`)
   - A password — this becomes `AMI_SECRET` in your `.env`
   - `read = system,call,originate` and `write = system,call,originate`
     permissions (Originate is what actually places the alert call)
   - **Restrict the Permit/Deny ACL to only the IP of the machine running
     server.py.** This is the real security boundary — AMI is effectively
     remote control of your phone system.

3. **Confirm AMI is actually listening on the network**, not just
   localhost:
   ```
   [general]
   enabled = yes
   port = 5038
   bindaddr = 0.0.0.0
   ```
   in `/etc/asterisk/manager.conf`, then `fwconsole reload` (or
   `asterisk -rx "manager reload"` on plain Asterisk). Verify with
   `ss -tlnp | grep 5038` — you want `0.0.0.0:5038`, not `127.0.0.1:5038`.

   ⚠️ **FreePBX regenerates this file on some upgrades.** If alerting
   silently stops working after an update, check this first.

4. In `config.yaml`, set `ami.host` to your Asterisk box's IP, and
   `ami.alert_channel` to the PJSIP channel you want rung on an alert
   (e.g. `PJSIP/100` for extension 100).

5. In `.env`, set `AMI_SECRET` (the AMI user's password) and `ALERT_UUID`
   (matching the UUID you put in the `[claude-phone-alert]` context).

## Common gotchas

- **`Connection refused` from server.py to the AMI port** ≠ a firewall
  issue. A firewall *drop* produces a hang-then-timeout; *refused* means
  the connection reached Asterisk and something said no — almost always
  AMI still bound to `127.0.0.1` (see step 3).
- **The alert call rings but plays the normal greeting, not the alert
  message** — `ALERT_UUID` in `.env` doesn't match the UUID in your
  `[claude-phone-alert]` context. Check server.py's log; the `UUID` line
  it prints shows exactly what arrived.
