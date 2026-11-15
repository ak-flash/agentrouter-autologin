# agentrouter-autologin

> **Русский:** [README.md](README.md)

Automatic daily login to [agentrouter.org](https://agentrouter.org) to collect the
daily $25 bonus + balance logging (so you can see whether the bonus is being credited).

Uses **zendriver** — async automation built on the Chrome DevTools Protocol.
Each run performs a full cycle: log out of the previous session → log in with
email/password → read the balance.

## Installation

Requires Python 3.10+ and an installed Google Chrome / Chromium.

### Proxy

Set `PROXY` in `.env` to route all Chrome traffic through a proxy:

```dotenv
AGENTROUTER_PROXY=http://127.0.0.1:8080
# https:// and socks5:// are also supported
# authenticated proxy: http://user:password@host:port
```

If unset, the browser connects directly. Proxy passwords are not written to the log.

**Windows (PowerShell):**
```powershell
cd e:\My-projects\agentrouter-autologin

# 1. Virtual environment (optional, but recommended)
python -m venv .venv
.\.venv\Scripts\activate

# 2. Dependencies
pip install -r requirements.txt
```

**Linux (bash):**
```bash
cd /path/to/agentrouter-autologin

# 1. Virtual environment (optional, but recommended)
python3 -m venv .venv
source .venv/bin/activate

# 2. Dependencies
pip install -r requirements.txt

# 3. Browser and Xvfb (for headful runs without a display)
sudo apt install -y chromium-browser xvfb
```

## Configuration

Copy `.env.example` to `.env` and fill in your details:

```powershell
Copy-Item .env.example .env
```

```ini
AGENTROUTER_EMAIL=you@example.com
AGENTROUTER_PASSWORD=your_password

# Save page HTML into pages/ for debugging the balance parser
SAVE_DEBUG_PAGES=false

# Minimum interval between successful runs, in seconds (default 86400 = 24h).
# Used with frequent cron runs so you don't log in more than once a day.
MIN_INTERVAL_SEC=86400
```

## Usage

### Daily run

```powershell
python autologin.py
```

The script:
1. Opens the browser (headful; on Linux via Xvfb).
2. If a session is active — logs out through the profile menu (Quit).
3. Logs back in with email/password.
4. Navigates to the profile page and reads the balance.
5. Writes the balance to `balance.txt` and the log, and the success timestamp to
   `last_success.txt`.

### Protection against repeated runs (24h marker)

After a successful balance read the script writes a timestamp to `last_success.txt`.
On the next run, if less than `MIN_INTERVAL_SEC` (default 24 hours) has passed since
the last success, the script **does nothing and does not launch the browser** — it
writes a "Skip" line to stdout (for cron) and to `logs/skip_status.txt`
(overwritten on each run), then exits.

This lets you schedule cron to run frequently (e.g. every 10 minutes): on a white
screen / failure the login is not recorded, and the next run retries; as soon as a
login succeeds, the browser is not launched again for 24 hours.

Repeated "Skip" lines are **not accumulated in `autologin.log`**: the current skip
status always lives in `logs/skip_status.txt` (one line, overwritten), while the
main log only keeps real events (login, balance, errors).

Force a run, bypassing the interval:

```powershell
python autologin.py --force
```

### Protection against concurrent runs (file lock)

If crontab accidentally has two entries (e.g. one from the user, another from root),
two processes start at the same time and duplicate every log line. The symptom is
identical messages milliseconds apart, and for Xvfb — two different display numbers
(`:1` and `:3`) in the same second.

To find duplicate cron entries on the server:

```bash
crontab -l          # current user's tasks
sudo crontab -l     # root's tasks
ls /etc/cron.d/     # system drop-in files
```

As of the current version the script is additionally protected by a file lock
(`autologin.lock`): if one process is already running, the second immediately exits
with the message "Skip: another process is already performing the login." (to stdout
and `logs/skip_status.txt`, without writing to the main log) and does not open the
browser. The lock is released automatically when the process exits.

Example output in `logs/autologin.log`:

```
2026-07-14 09:00:01 | INFO | Balance: $25.00 (first record)
2026-07-15 09:00:02 | INFO | Balance: $50.00 (previous: $25.00, change: +$25.00)
2026-07-15 09:00:02 | INFO | Bonus credited: +$25.00
```

### Headless mode (optional)

```powershell
python autologin.py --headless
```

Usually not needed — the site requires a real browser to log in.

## Auto-run

Thanks to the 24h marker (`last_success.txt`) the script can be run frequently — it
skips runs automatically if a login already succeeded today. The recommended option is
every 10 minutes: this increases the chance of catching a working page during white
screens.

### Windows (Task Scheduler)

Create the task via PowerShell (as administrator):

```powershell
$action = New-ScheduledTaskAction `
  -Execute "e:\My-projects\agentrouter-autologin\.venv\Scripts\python.exe" `
  -Argument "autologin.py" `
  -WorkingDirectory "e:\My-projects\agentrouter-autologin"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "AgentRouterAutologin" -Action $action -Trigger $trigger -Force
```

Or manually: Task Scheduler → Create Task → Trigger "Daily", repeat every 10 minutes →
Action to run `python.exe` with the argument `autologin.py`.

### Linux (cron)

Run every 10 minutes (the script starts Xvfb itself, since cron has no `DISPLAY`):

```cron
*/10 * * * * cd /home/ubuntu/agentrouter-autologin && /home/ubuntu/agentrouter-autologin/.venv/bin/python autologin.py >> /home/ubuntu/agentrouter-autologin/logs/cron.log 2>&1
```

Important:
- Use the **full path** to the venv python — cron does not activate the venv itself.
- `cd` into the project folder is needed so that `balance.txt`, `last_success.txt`,
  and `logs/` are created next to it.
- For headful work via Xvfb on a server, `chromium-browser`/`google-chrome` and
  `xvfb` must be installed.

## Debugging the balance parser

If the balance is not found, enable HTML saving in `.env`:

```ini
SAVE_DEBUG_PAGES=true
```

The script will save the page and screenshots into `pages/`. Offline parsing of saved
pages:

```powershell
# the latest saved file
python autologin.py --parse-latest

# a specific file
python autologin.py --parse-html pages\dashboard_2026-07-14_090001.html
```
