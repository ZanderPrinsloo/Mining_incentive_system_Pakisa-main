# Deploying the Pakisa Dashboard to a company server

The app is a Flask dashboard (Waitress WSGI server) that reads from the SQL
Server reporting database (STPTM2000). This guide deploys it as a **Windows
service** that starts automatically on boot and is reachable on the intranet
at `http://<server>:5002`.

---

## Before you start

You need on the server:

1. **Python 3.12+** installed for your user.
2. **ODBC Driver 17 for SQL Server** — the app talks to SQL Server via ODBC.
   Check with:
   ```bat
   reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server"
   ```
   If missing, install it from
   https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
   (ask IT if you don't have rights).
3. **Network access** from the server to the SQL Server host (e.g.
   `HGSQLHRT001` — confirm with whoever administers the database).
4. **STPTM2000 must already have the dashboard's database objects.** Run
   `sql\DEPLOY_usp_RebuildIncentiveViews_STPTM2000.sql` against it first if
   that hasn't been done yet (see that file's own header for details) — the
   dashboard will not work without it.
5. The **`.env` file** with the database credentials. It is NOT in git
   (secrets) — create it in the project folder on the server. Content:
   ```
   DB_SERVER=HGSQLHRT001
   DB_DATABASE=STPTM2000
   DB_USERNAME=<your-sql-username>
   DB_PASSWORD=<your-sql-password>
   ```
   Get the real username/password from whoever administers the SQL Server —
   do not reuse another project's credentials without checking they're
   meant to be shared. Leave `DB_USERNAME`/`DB_PASSWORD` out entirely to use
   Windows authentication instead (the service account's own login) — see
   `.env.example` and `config\config.yaml` for the full set of options.

---

## Deployment steps

Copy the whole project folder to the server (e.g. `C:\inetpub\pakisa` or any
folder you can write to). Then on the server:

### 1. One-time setup (elevated Command Prompt)

```bat
cd C:\<path-to-project>
deploy\setup.bat
```

This creates the virtual environment, installs dependencies
(`requirements.txt` + `pywin32`), and opens firewall port 5002. Needs
internet for pip.

### 2. Install and start the service

```bat
deploy\install_service.bat
```

By default the service runs as **LocalSystem**. The app authenticates to SQL
Server with the login in `.env`, so LocalSystem is fine. If you must run it
as your own account instead:

```bat
deploy\install_service.bat DOMAIN\youruser yourpassword
```

### 3. Verify

On the server: open `http://localhost:5002`.
From another PC: open `http://<server-name>:5002`.

To uninstall: `deploy\uninstall_service.bat`.

---

## Day-to-day operations

| Task                     | Command                                                      |
| ------------------------ | ------------------------------------------------------------ |
| Start service            | `.venv\Scripts\python.exe run_service.py start`              |
| Stop service             | `.venv\Scripts\python.exe run_service.py stop`               |
| Restart service          | `.venv\Scripts\python.exe run_service.py restart`            |
| Run in foreground (test) | `deploy\start.bat`  (stop with `deploy\stop.bat`)            |
| Check logs               | `logs\pakisa.log` in the project root                        |

The service appears as **"Pakisa Stoping Analysis Dashboard"** in
services.msc.

---

## Updating to a new version

```bat
cd C:\<path-to-project>
git pull
deploy\setup.bat                      :: re-installs any new dependencies
.venv\Scripts\python.exe run_service.py restart
```

If the update includes SQL changes (new columns, a changed stored
procedure), re-run the relevant script in `sql\` against STPTM2000 first —
check that script's own header for whether it's safe to re-run blind or
needs a DBA's review.

---

## Configuration

`config\config.yaml` and the `.env` file control behaviour:

- `DB_SERVER` / `DB_DATABASE` — SQL Server instance and database. Pakisa
  needs `DB_DATABASE=STPTM2000` specifically (the app defaults to this if
  unset, but set it explicitly in `.env` to be certain on a shared server).
- `DB_USERNAME` / `DB_PASSWORD` — SQL login (leave unset for Windows auth).
- `HOST` / `PORT` — defaults `0.0.0.0:5002`; override via environment
  variables if 5002 conflicts (e.g. with Tshepong's own dashboard on 5001
  running on the same machine).
- Dashboard targets and saved bonus-policy scenarios are stored locally in
  `data\dashboard_state.db` (SQLite) — back this file up if you care about
  them.

## Troubleshooting

- **`http://<server>:5002` works on the server but not from other PCs** —
  firewall rule missing or blocked by corporate policy. Re-run
  `deploy\setup.bat` elevated, or have IT open TCP 5002.
- **Pages load but show errors in the charts** — check the SQL Server is
  reachable from the server: `ping HGSQLHRT001` and verify the ODBC driver
  version matches `config\config.yaml` (`driver:`). Also confirm
  `DB_DATABASE` is actually `STPTM2000` and not defaulting to the wrong
  database if this server also hosts other dashboards.
- **Service won't start** — check Windows Event Viewer (Application log,
  source `PakisaDashboard`); recent errors are also in `logs\`.
