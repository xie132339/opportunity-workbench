# Deploy the opportunity workbench to Aliyun ECS

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

Publish the current `feature/bootstrap` worktree on the user-created Aliyun ECS in Shanghai. The public IP must serve the login-protected workbench over a reverse proxy, while the Taobao worker continues in a separately supervised process. The SQLite database, `.env`, runtime state, logs, and backups must remain on persistent disk.

## Scope, constraints and assumptions

- Target is the user-owned Aliyun ECS shown in the console: Shanghai, Ubuntu 24.04, 2 vCPU / 2 GiB, 40 GiB disk, 3 Mbps public bandwidth, public IP `8.159.128.186`.
- Current implementation branch is `feature/bootstrap`; protected branches are never changed.
- The source development script binds only to localhost, so production needs a reverse proxy plus an explicitly configured app listener; the worker remains private.
- No Docker image is introduced because repository policy defers Docker until business scenarios are complete.
- No credentials, SQLite file, or `.env` are committed or copied into public output. A server-specific strong `WORKBENCH_ADMIN_PASSWORD` is required before public exposure.
- User authorization covers creating deployment files and server setup. Payment, domain registration, ICP filing, and revealing credentials remain user-controlled.

## Context and orientation

`app.py serve` runs the Flask web service and `app.py taobao-worker` independently. `scripts/workbench.sh` is a local process manager and performs local health checks. `data/workbench.sqlite3` is the application database. The local login gate lives in `services/local_auth.py` and reads credentials from `.env`.

## Progress

- [x] (2026-10-12 Asia/Shanghai) User created and started the ECS instance; console shows Ubuntu 24.04, public IP `8.159.128.186`, 3 Mbps bandwidth.
- [x] (2026-10-12 Asia/Shanghai) Read existing deployment constraints and confirmed `feature/bootstrap` is a non-protected branch.
- [x] (2026-10-12 Asia/Shanghai) Installed Workbench CLI v1.0.1; CLI list and read-only `hostname` execution succeeded for `i-uf693wtee99tze11foca`.
- [x] (2026-10-12 Asia/Shanghai) Added production Gunicorn, systemd, Nginx, environment contract, and SQLite online-backup artifacts; local and remote verification remains pending.
- [ ] Copy the approved source revision without local secrets; create server `.env` using server-owned credentials.
- [ ] Start services and verify local health, public HTTP redirect-to-login, authenticated page rendering, worker health, persistence after restart, and backup restore metadata.

## Plan of work

1. **Remote access.** Install Workbench CLI locally. Use an existing least-privilege credential profile or the user-console remote terminal to confirm the instance ID, Cloud Assistant status, OS, filesystem capacity, and network access.
2. **Production package.** Add a WSGI entrypoint, Gunicorn service, dedicated worker service, Nginx configuration, environment file contract, and a SQLite backup timer. Bind application traffic to loopback and expose only 80/443 through Nginx.
3. **Server setup.** Create an unprivileged service account and `/application`; install only OS/Python/Nginx dependencies required by the package; clone/check out the approved feature branch; create `.venv`, `.env`, data, state, logs, and backup directories with restrictive ownership.
4. **Activation.** Install systemd units, configure Nginx, restrict security-group ingress to SSH/HTTP/HTTPS, start services, and capture status/log evidence.
5. **Acceptance.** Exercise public IP HTTP behavior, login, health check, worker process, service restart, SQLite integrity/backup, and non-exposure of port 5002. Record exact evidence and remaining domain/ICP boundary.

## Validation and acceptance

- `systemctl is-active` reports active for web, worker, Nginx, and backup timer.
- `curl -I http://127.0.0.1:<internal-port>/health` returns 200 locally; remote public port 5002 is unreachable.
- `curl -I http://8.159.128.186/` redirects unauthenticated clients to `/login`; authenticated browser access reaches the workbench.
- Worker logs show a started, current source process without secret output.
- SQLite `PRAGMA integrity_check` returns `ok`; a dated backup exists and has a recorded checksum/size.

## Surprises and discoveries

- The first configured RAM key returned a cryptographic signature failure. After the user replaced it, `workbench list ecs` and read-only `workbench exec` both succeeded; deployment now proceeds through CLI only.

## Decision log

- 2026-10-12: use source + virtual environment + systemd, not Docker, to remain consistent with repository policy and avoid Docker-first operational changes.
- 2026-10-12: run the web app and Taobao worker separately, so a worker failure cannot take down the user interface.

## Idempotence and recovery

- All server paths are scoped under `/application` and `/var/lib/opportunity-workbench`; repeated deployment updates the application revision but preserves `.env`, SQLite data, and backups.
- Before any overwrite, inspect target file/service state. Roll back to the prior Git commit and restart only the project systemd units.
- Do not remove database files; restore only from an explicitly selected dated backup after integrity review.

## Outcomes and retrospective

Pending remote connection and live deployment evidence.
