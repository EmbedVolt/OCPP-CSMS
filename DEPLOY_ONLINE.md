# Deploy the OCPP Test CSMS Online

## Recommended setup

Use **GitHub + Railway** for this lab CSMS.

The application uses one HTTP port for both the dashboard/API and the OCPP WebSocket endpoint. Railway supplies HTTPS/TLS automatically, so a charger connects with `wss://`.

## 1. Push to GitHub

Create a new **private** GitHub repository, then push the contents of this folder so that `Dockerfile` is at the repository root.

```bash
git init
git add .
git commit -m "Initial OCPP test CSMS"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

Do not commit passwords, charger credentials, private keys, or production certificates.

## 2. Deploy on Railway

1. Create a Railway project.
2. Add a service from the GitHub repository.
3. Railway will detect the root `Dockerfile` automatically.
4. Open **Settings > Networking > Public Networking** and generate a Railway domain.
5. Set the health check path to:

```text
/api/health
```

The app now reads Railway's `PORT` variable automatically.

## 3. Persist SQLite history

Without a volume, the CSMS still works, but SQLite history can disappear after a redeploy/restart.

Attach a Railway Volume to:

```text
/app/data
```

The existing database path `data/csms.sqlite3` will then be persistent.

## 4. Open the dashboard

If Railway gives you:

```text
https://my-ocpp-csms.up.railway.app
```

open that URL in the browser.

Health test:

```text
https://my-ocpp-csms.up.railway.app/api/health
```

## 5. Connect a real charger

Use:

```text
wss://my-ocpp-csms.up.railway.app/ocpp/CHARGER_ID
```

Examples:

```text
wss://my-ocpp-csms.up.railway.app/ocpp/EVC5-001
wss://my-ocpp-csms.up.railway.app/ocpp/DC30-LAB
```

The charger must negotiate the matching WebSocket subprotocol:

```text
OCPP 1.6   -> ocpp1.6
OCPP 2.0.1 -> ocpp2.0.1
OCPP 2.1   -> ocpp2.1
```

Do not use `ws://` over the public internet. Use `wss://`.

## 6. Expected online test

After connecting a charger:

1. Charger opens the WSS connection.
2. CSMS identifies the station from `/ocpp/{stationIdentity}`.
3. Charger sends `BootNotification`.
4. Dashboard shows the charger as online.
5. `StatusNotification` / `TransactionEvent` / meter traffic appears in Messages.
6. Select the charger in **OCPP Console**.
7. Send `Reset`, `TriggerMessage`, charging-profile commands, diagnostics commands, certificate messages, etc.
8. Inspect `CALL`, `CALLRESULT`, and `CALLERROR` frames from the dashboard.

## Important security limitation

The current test dashboard has no user authentication. A public Railway URL therefore exposes the test UI/API to anyone who discovers the URL.

For short lab tests this can be acceptable if you remove/disable the deployment afterward. Before keeping it online permanently, add dashboard authentication and charger authentication/security-profile handling.

## Alternative: Render

Render also supports GitHub deployment, managed TLS, and public WebSockets. The same application can be deployed there after the included `PORT` compatibility change.

For SQLite persistence on Render, use a persistent disk or replace SQLite with a managed database.
