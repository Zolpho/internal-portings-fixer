# Internal Portings Fixer

Small FastAPI web app to correct “internal porting” inconsistencies across:
- **ENP**: Postgres `numberpool` / table `numbers`
- **nprn-routing**: Redis (DB 9) key `nprn:routing:<dn>`
- **pSuite / dispatcher-api2**: MariaDB table `cli_provisioning`

The workflow and fixes are based on the internal “Internal portings” procedure (Scenario 1: wholesale 98019 → NXP1 98067)
https://wiki.nexphone.ch/display/NETVOIP/Internal+portings

## What it fixes

### fix ENP
Updates the ENP `numbers` row for each DN:
- `reservation_tstamp = '2050-01-01 00:00:00'`
- `product_id = 1`
- `system_id = 500`
- `nprn = 98067`
- `outporting_tstamp = NULL`
- `lastupdated_tstamp = NOW()`

### fix nprn-routing
Deletes the Redis routing key(s) (Redis DB 9):
- `DEL nprn:routing:<dn>`

### fix disp routing
Deletes all `cli_provisioning` rows matching the `target_number` (single or range) so that the number(s) can be reprovisioned in pSuite rich client afterward.

## Input formats (single or range)

The UI input accepts:
- Single number: `0449510080`
- Range: `0449510080-89` (expands to `0449510080 ... 0449510089`)

Safety limit: maximum expanded range size is **100** numbers.

### Normalization rules
For each item in the expanded list:
- `target_number` is kept in national format with leading `0` (e.g. `0449510080`) used by `cli_provisioning`. 
- `dn` is derived as E.164 digits without `+` (e.g. `41449510080`) used by ENP and Redis key naming.

## Dry-run mode

A **dry-run** checkbox exists in the UI.

When enabled, endpoints do not modify anything and return:
- `expanded_targets`: list of `target_number` that would be affected
- `expanded_dns`: list of `dn` that would be affected
- `expanded_redis_keys`: list of Redis keys that would be deleted

Additionally for `/fix/disp`, dry-run returns the DB rows that would be deleted (`would_delete_rows`).

## API endpoints

All endpoints require an API token header:
- Header: `x-api-token: <API_TOKEN>`

Endpoints:
- `GET /` → HTML UI
- `POST /fix/enp` → Postgres update (or dry-run)
- `POST /fix/nprn` → Redis delete (or dry-run)
- `POST /fix/disp` → MariaDB delete (or dry-run)

### Request body
```json
{
  "input": "0449510080-89",
  "dry_run": true
}

## Project layout

# Recommended structure:
```
internal-portings-fixer/
  app.py
  requirements.txt
  .env
  static/
    index.html
```
## Installation (Linux VM)

The app is designed to run on a Linux VM on the same LAN with network reachability to:

1) ENP Postgres host

2) Redis host (nprn-routing, DB 9)

3) dispatcher-api2 MariaDB host

# 1) System packages

Ubuntu/Debian example:
```
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

# 2) Create directory

```
sudo mkdir -p /opt/github/
sudo chown -R $USER:$USER /opt/github/
cd /opt/github
```

# 3) Clone repository
```
git clone https://github.com/Zolpho/internal-portings-fixer.git
```

# 4) Create venv and install deps

```python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

# 5) Configure environment

```
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```
Create `/opt/github/internal-portings-fixer/.env`:

```
# Web
BIND_HOST=0.0.0.0
BIND_PORT=8000
API_TOKEN=change-me-long-random

# Postgres (ENP numberpool)
PG_DSN="host=YOUR_PG_HOST dbname=numberpool user=YOUR_USER password=YOUR_PASS port=5432"

# Redis (nprn-routing)
REDIS_URL="redis://YOUR_REDIS_HOST:6379/0"
REDIS_DB=9

# MariaDB (dispatcher-api2 / pSuite)
MDB_HOST=YOUR_MDB_HOST
MDB_PORT=3306
MDB_USER=YOUR_USER
MDB_PASS=YOUR_PASS
MDB_DB=dispatcher-api2
```

# 6) Run in foreground (test)

```
cd /opt/internal-portings-fixer
set -a; source .env; set +a
./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Open: `http://<VM_IP>:8000/`

## Run as a systemd service

# 1) Create a service user

```
sudo useradd -r -s /usr/sbin/nologin portfix || true
sudo chown -R portfix:portfix /opt/github/internal-portings-fixer
```

# 2) Create systemd unit

Create `/etc/systemd/system/internal-portings-fixer.service`:
```
[Unit]
Description=Internal portings fixer
After=network.target

[Service]
User=portfix
Group=portfix
WorkingDirectory=/opt/github/internal-portings-fixer
EnvironmentFile=/opt/github/internal-portings-fixer/.env
ExecStart=/opt/github/internal-portings-fixer/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

# 3) Enable and start

```
sudo systemctl daemon-reload
sudo systemctl enable --now internal-portings-fixer
sudo systemctl status internal-portings-fixer
```

# Logs

```
sudo journalctl -u internal-portings-fixer -f
```

# Security notes

This app can update/delete production routing state across multiple systems.

Recommended hardening:

- Bind to a LAN interface only (or firewall to specific admin subnets).

- Keep `API_TOKEN` secret and rotate it.

- Consider putting it behind Nginx with IP allowlists and basic auth.

# Operational notes / expected workflow

Typical workflow for a number or range:

Enable dry-run and press each button to confirm which numbers/keys/rows will be touched.

Run:

- fix ENP

- fix nprn-routing

- fix disp routing

Go to pSuite rich client and reprovision the number(s).

# Troubleshooting

401 Unauthorized: missing/incorrect `x-api-token`.

400 Unsupported number format: input is not in `0XXXXXXXXX` (CH national) or `41XXXXXXXXX` (E.164 digits) form.

Range too large (>100): reduce range size.

DB connection errors: verify `.env` hosts/ports/credentials and VM LAN routing/firewall. 
