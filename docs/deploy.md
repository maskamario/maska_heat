# Deploying on the Raspberry Pi 3 B+

## 1. Prepare the SD card

Use **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)*. In the settings (gear icon):

- Hostname: `maska-heat`
- Username + password of your choice (below: `mario`)
- Enable SSH
- Locale: timezone `Europe/Tallinn`
- WiFi: not needed — the Pi is on Ethernet

Plug in Ethernet + power. In the router, reserve a fixed address for the Pi (e.g. `192.168.1.10`).

## 2. Base system

```bash
ssh mario@maska-heat.local
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv sqlite3
```

Cap the journal so logs never fill the SD card:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf "[Journal]\nSystemMaxUse=100M\n" | sudo tee /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
```

## 3. Install the app

```bash
git clone https://github.com/maskamario/maska_heat.git ~/maska_heat
cd ~/maska_heat
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"
```

Copy your configuration from the laptop (run on the laptop, in the repo folder):

```bash
scp config.yaml secrets.yaml mario@maska-heat.local:~/maska_heat/
```

Check every source from the Pi before starting the service:

```bash
.venv/bin/python -m maska_heat.probe prices
.venv/bin/python -m maska_heat.probe mill
.venv/bin/python -m maska_heat.probe adax
.venv/bin/python -m maska_heat notify-test
```

## 4. Run as a service

```bash
sed "s/__USER__/$USER/g" deploy/maska-heat.service | sudo tee /etc/systemd/system/maska-heat.service
sudo systemctl daemon-reload
sudo systemctl enable --now maska-heat
systemctl status maska-heat
journalctl -u maska-heat -f        # live log, Ctrl+C to leave
```

Dashboard: **http://maska-heat.local:8000** (or `http://192.168.1.10:8000`) from any phone or laptop at home.

## 5. Notifications (ntfy)

1. Generate a topic name (it acts as the password, keep it private):
   `python3 -c "import secrets;print('maska-' + secrets.token_urlsafe(16))"`
2. Put it in `secrets.yaml` → `ntfy: topic:`.
3. Install the **ntfy** app on both phones → *Subscribe to topic* → same topic name.
4. `sudo systemctl restart maska-heat`, then press *Send test notification* on the dashboard.

## 6. Updating

```bash
cd ~/maska_heat
git pull
.venv/bin/pip install -e ".[all]"
sudo systemctl restart maska-heat
```

## 7. Backup

The database is `~/maska_heat/data/maska_heat.db`. Safe copy while running:

```bash
sqlite3 ~/maska_heat/data/maska_heat.db ".backup '/home/mario/maska_heat-backup.db'"
```

## Notes

- The Mill IP moves from `192.168.1.229` to the reserved `192.168.1.12` when its lease renews — update
  `devices.matis_mill.host` in `config.yaml` then and restart the service.
- Enable `living_mitsubishi` and the ESPHome sensors in `config.yaml` once they are set up.
- The dashboard loads htmx and Chart.js from a CDN, so the viewing device needs internet; the Pi itself keeps
  logging without it (except prices and weather).
