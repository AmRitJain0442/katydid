# Continuous Vultron operation

`scripts/run_service.py` starts the installed platform with branch watching and
daily scheduled checks, writes rotating local logs (five files, up to 5 MB each),
and propagates the worker exit status to its OS supervisor. Model credentials
stay on the host. Log files are operational evidence and should remain private.

## Windows

After locked environment setup, install a task for the current user:

```powershell
powershell -NoProfile -File scripts/install_service.ps1 -Fleet C:\vultron-state\fleet.yaml -Logs C:\vultron-state\logs -CredentialFile C:\private\google-credentials.json
Start-ScheduledTask -TaskName Vultron-Worker
Get-ScheduledTaskInfo -TaskName Vultron-Worker
```

Omit `-CredentialFile` when using Codex's existing authentication. The task starts
at this user's logon, prevents concurrent instances, and retries process failures
up to ten times at one-minute intervals. It does not run while the user is logged
out, and sleeping or powering off the host stops monitoring. A dedicated always-on
host is required for unattended availability independent of a developer session.
The installer refuses to overwrite an existing task.

Use the dashboard's Runtime and checks panel or `GET /api/runtime` to inspect the
worker, selected provider, discovery interval, schedule, and required checks.
`worker_alive` means the worker thread exists; test outcomes remain in task evidence.

Before changing fleet configuration or restarting, let tasks finish or pause them.
Forced process termination can leave externally committed actions unresolved; the
existing lease/reconciliation rules still apply. Logs are in the selected logs
directory. To uninstall only this registration:

```powershell
Stop-ScheduledTask -TaskName Vultron-Worker
Unregister-ScheduledTask -TaskName Vultron-Worker -Confirm:$false
```

## Linux

Run the same launcher under a dedicated systemd service account with access to its
own Git, model authentication, fleet state, and approved tools. For example:

```ini
[Unit]
Description=Vultron autonomous testing worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vultron
WorkingDirectory=/opt/vultron
ExecStart=/opt/vultron/.venv/bin/python /opt/vultron/scripts/run_service.py --fleet /var/lib/vultron/fleet.yaml --logs /var/lib/vultron/logs
Restart=on-failure
RestartSec=30
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

Paths and the service account must exist before enabling the unit. This example
does not install a Linux server, public endpoint, or deployment credentials.
