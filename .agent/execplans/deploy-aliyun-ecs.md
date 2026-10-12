# Deploy the opportunity workbench to Aliyun ECS

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

Publish the current `feature/bootstrap` worktree on the user-created Aliyun ECS in Shanghai. The public IP serves the login-protected workbench over a reverse proxy, while the Taobao worker continues in a separately supervised process. Application source, runtime state, environment file, SQLite database, and backups live under `/application`; only operating-system-owned Nginx and systemd configuration stays in `/etc`.

## Scope, constraints and assumptions

- Target is the user-owned Aliyun ECS shown in the console: Shanghai, Ubuntu 24.04, 2 vCPU / 2 GiB, 40 GiB disk, 3 Mbps public bandwidth, public IP `8.159.128.186`.
- Current implementation branch is `feature/bootstrap`; protected branches are never changed.
- The source development script binds only to localhost, so production needs a reverse proxy plus an explicitly configured app listener; the worker remains private.
- No Docker image is introduced because repository policy defers Docker until business scenarios are complete.
- No credentials, SQLite file, or runtime environment file are committed or copied into public output. The server environment file remains readable only to `root` and the `workbench` service group.
- User authorization covers creating deployment files and server setup. Payment, domain registration, ICP filing, and revealing credentials remain user-controlled.

## Context and orientation

`app.py serve` runs the Flask web service and `app.py taobao-worker` independently. `scripts/workbench.sh` is a local process manager and performs local health checks. `data/workbench.sqlite3` is the application database. The local login gate lives in `services/local_auth.py` and reads credentials from `.env`.

## Progress

- [x] (2026-10-12 Asia/Shanghai) User created and started the ECS instance; console shows Ubuntu 24.04, public IP `8.159.128.186`, 3 Mbps bandwidth.
- [x] (2026-10-12 Asia/Shanghai) Read existing deployment constraints and confirmed `feature/bootstrap` is a non-protected branch.
- [x] (2026-10-12 Asia/Shanghai) Installed Workbench CLI v1.0.1; CLI list and read-only `hostname` execution succeeded for `i-uf693wtee99tze11foca`.
- [x] (2026-10-12 Asia/Shanghai) Added production Gunicorn, systemd, Nginx, runtime-environment contract, and SQLite online-backup artifacts; local script/backup checks passed.
- [x] (2026-10-12 Asia/Shanghai) Cloned `feature/bootstrap` to `/application`; installed Python dependencies, Nginx, and systemd units through Workbench CLI.
- [x] (2026-10-12 Asia/Shanghai) Fixed root-owned SQLite initialization. The live database is `workbench:workbench` at `/application/.runtime/workbench.sqlite3`.
- [x] (2026-10-12 Asia/Shanghai) Migrated the environment file, active database, and dated backups to `/application/.runtime`; the previous copies are retained in `/application/.runtime/legacy-20261012` and old runtime paths no longer exist.
- [x] (2026-10-12 Asia/Shanghai) Started web, worker, backup timer, and Nginx. Internal health returns 200; a CSRF-protected login with the requested initial account returns 302 and then reaches the homepage with 200.
- [x] (2026-10-12 Asia/Shanghai) Verified Nginx listens on `0.0.0.0:80` and Gunicorn only on `127.0.0.1:5002`. External TCP connection to port 80 succeeded; port 5002 is rejected from both the ECS private and public addresses.

## Plan of work

1. **Remote access.** Install Workbench CLI locally. Use an existing least-privilege credential profile or the user-console remote terminal to confirm the instance ID, Cloud Assistant status, OS, filesystem capacity, and network access.
2. **Production package.** Add a WSGI entrypoint, Gunicorn service, dedicated worker service, Nginx configuration, environment file contract, and a SQLite backup timer. Bind application traffic to loopback and expose only 80/443 through Nginx.
3. **Server setup.** Create an unprivileged service account and `/application`; install only OS/Python/Nginx dependencies required by the package; clone/check out the approved feature branch; create `.venv`, `.env`, data, state, logs, and backup directories with restrictive ownership.
4. **Activation.** Install systemd units, configure Nginx, restrict security-group ingress to SSH/HTTP/HTTPS, start services, and capture status/log evidence.
5. **Acceptance.** Exercise public IP HTTP behavior, login, health check, worker process, service restart, SQLite integrity/backup, and non-exposure of port 5002. Record exact evidence and remaining domain/ICP boundary.

## Validation and acceptance

- `systemctl is-active` reports active for web, worker, Nginx, and backup timer.
- PASS — `systemctl is-active` reports active for web, worker, Nginx, and backup timer.
- PASS — `curl http://127.0.0.1:5002/health` returns 200. Nginx returns 302 to `/login` for unauthenticated local HTTP. Gunicorn only binds to `127.0.0.1:5002`.
- PASS — login page CSRF token plus the requested initial credential returns 302; its resulting session obtains 200 from the homepage.
- PASS — dated online backup at `/application/.runtime/backups/workbench-20261012T065335Z.sqlite3` is 274432 bytes; its script performs `PRAGMA integrity_check` before atomic publish. No temporary `*.partial*` files remain in the active backup directory.
- PARTIAL — external TCP connectivity to port 80 was observed, but this execution environment's direct public HTTP client received an empty response. The normal external-browser HTTP response remains to be confirmed outside this execution environment. This does not affect the verified local Nginx route or application services.

## Surprises and discoveries

- The first configured RAM key returned a cryptographic signature failure. After the user replaced it, `workbench list ecs` and read-only `workbench exec` both succeeded; deployment now proceeds through CLI only.
- The initial installer validated WSGI as `root`, producing a root-owned SQLite database and a real `attempt to write a readonly database` failure. The installer now validates as `workbench`, and the live database ownership was repaired before restart.
- SQLite online backup produced temporary WAL/SHM sidecars next to its `.partial` database. The script now removes all temporary sidecars after the integrity check; the live backup directory was rechecked.

## Decision log

- 2026-10-12: use source + virtual environment + systemd, not Docker, to remain consistent with repository policy and avoid Docker-first operational changes.
- 2026-10-12: run the web app and Taobao worker separately, so a worker failure cannot take down the user interface.
- 2026-10-12: keep all project-owned files in `/application`, using `/application/.runtime` for the environment, SQLite data, and backups. Systemd and Nginx configuration remain in their operating-system paths because those daemons require them there.

## Idempotence and recovery

- All project-owned server paths are scoped under `/application`; repeated deployment updates source while preserving `/application/.runtime`.
- Before any overwrite, inspect target file/service state. Roll back to the prior Git commit and restart only the project systemd units.
- Do not remove database files; restore only from an explicitly selected dated backup after integrity review.

## Outcomes and retrospective

The current `feature/bootstrap` revision is running from `/application`. Web, worker, backup timer, Nginx, local health, login, database ownership, and active backup behavior have current evidence. The remaining boundary is only independent external-browser HTTP confirmation; this execution environment reached port 80 at TCP level but its direct HTTP client returned no response body.
