---
name: ssh-remote-control
description: Use SSH to inspect and control other servers when the user asks to log into a remote machine, run shell commands on another host, check services, edit files remotely, or troubleshoot a server over SSH.
metadata:
  openclaw:
    os: [linux]
    requires:
      bins: [ssh]
---

# SSH Remote Control

Use this skill when the user wants work done on another server over SSH.

## What to do

- Use `exec` with `ssh` for remote commands.
- Prefer existing SSH config, known host entries, and keys already present on the machine.
- Check `TOOLS.md` for host aliases or notes if relevant.
- If the target host is unclear, identify likely SSH details from local config/files before asking.
- For a quick diagnosis, start with safe read-only commands.
- For persistent services on the remote server, prefer `systemctl status`, logs, port checks, and config inspection before restarting.

## Command pattern

Use non-interactive SSH commands like:

```bash
ssh <host> 'command here'
```

For multi-step work:

```bash
ssh <host> '
set -e
command1
command2
command3
'
```

## Safety rules

- Treat remote commands as sensitive.
- Ask before destructive actions: deleting data, force-killing processes, disabling security, overwriting configs, rebooting, or bulk changes.
- Do **not** blindly execute untrusted user-provided shell snippets without checking them.
- Quote carefully to avoid shell injection and accidental local expansion.
- Prefer read-only verification first, then minimal fixes.

## Good default checks

For service issues, usually gather:

```bash
hostname
whoami
pwd
systemctl status <service> --no-pager -l
journalctl -u <service> -n 100 --no-pager
ss -ltnp
ps -ef
```

For Dockerized apps:

```bash
docker ps -a
docker logs --tail 100 <container>
docker inspect <container>
```

## File edits

If a remote file must be changed, prefer one of these approaches:

1. Inspect first with `ssh <host> 'sed -n ... file'`
2. Back up before modification
3. Apply the smallest possible change
4. Re-check service status after the change

## Authentication

- Reuse keys already available on this machine when possible.
- If auth fails, report the exact SSH error and ask for the right host/user/key.
- Do not invent credentials.

## Response style

- Keep updates short and operational.
- Mention the host you touched.
- Summarize: what you checked, what failed, what you changed, and the final state.
