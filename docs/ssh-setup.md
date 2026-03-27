# Git SSH Authentication

Koan uses `git fetch` and `git push` to interact with GitHub repositories.
This guide covers how to set up SSH authentication for each deployment mode.

## Quick Reference

| Deployment mode | SSH agent | Fallback SSH key | HTTPS / GH_TOKEN |
|-----------------|-----------|------------------|-------------------|
| **macOS (direct)** | ✅ Inherited automatically | Optional | Via `gh` CLI |
| **Linux systemd** | ✅ Via `make ssh-forward` | Recommended | Via `.env` |
| **Docker** | ✅ Via socket mount | Via `~/.ssh` mount | Via `make docker-gh-auth` |

## Concepts

**Two-layer authentication** — Koan uses SSH agent forwarding as the primary
method (your existing keys from your laptop), with a server-side fallback key
for when you're not connected:

1. **SSH agent** — tried first. Uses keys loaded in your SSH agent (forwarded
   via `ssh -A` or local).
2. **Fallback key** — tried automatically when the agent is unavailable. A
   passphrase-less SSH key stored on the server, added to your GitHub account.

The SSH client handles this fallback automatically — no special git
configuration needed.

---

## Scenario 1: macOS — Direct Run (Simplest)

When you run Koan directly (`make run` / `make start` via pid_manager), the
processes inherit your shell's SSH agent. No special setup needed.

```bash
# Verify your SSH agent is running and has keys loaded
ssh-add -l

# If empty, add your key
ssh-add ~/.ssh/id_ed25519

# Start Koan — it inherits SSH_AUTH_SOCK automatically
make start
```

**Optional fallback key:** If you close the terminal, the agent may stop. To
keep Koan working autonomously, set up a fallback key (see
[Generating a Fallback Key](#generating-a-fallback-key) below).

---

## Scenario 2: Linux systemd — Without Fallback Key

SSH agent forwarding works through `ssh -A`. When you run `make start`, Koan
captures the agent socket path for the systemd service.

```bash
# 1. SSH into the server with agent forwarding
ssh -A user@your-server

# 2. Start Koan — automatically forwards SSH agent socket
make start
```

`make start` creates a symlink (`.ssh-agent-sock`) from your current
`SSH_AUTH_SOCK` to a stable path that the systemd service references.

**After reconnecting** (new SSH session), refresh the agent socket:

```bash
# Update the symlink to point to your new SSH agent socket
make ssh-forward
```

No service restart needed — the symlink is updated in-place.

**Limitation:** When you disconnect, the agent socket dies. Git operations
will fail until you reconnect and run `make ssh-forward`. For autonomous
operation, add a fallback key (next scenario).

---

## Scenario 3: Linux systemd — With Fallback Key (Recommended)

Same as Scenario 2, plus a server-side fallback key. This is the recommended
setup for production servers — Koan works autonomously even when you're not
connected.

### Step 1: Generate the fallback key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/koan_id_ed25519 -N "" -C "koan-fallback@$(hostname)"
```

This creates a passphrase-less key. The empty passphrase (`-N ""`) is
intentional — the key must be usable without interactive input.

### Step 2: Add the key to GitHub

```bash
# Display the public key
cat ~/.ssh/koan_id_ed25519.pub
```

Go to [GitHub → Settings → SSH and GPG keys](https://github.com/settings/keys)
and add the public key. Give it a descriptive title (e.g., "Koan fallback —
myserver").

> **Note:** GitHub deploy keys are per-repository. Since Koan works with
> multiple repos across orgs, adding the key to your **GitHub account** (not
> as a deploy key) is simpler — one key covers all repos you have access to.

### Step 3: Configure SSH to use the fallback key

Add to `~/.ssh/config` on the server:

```
Host github.com
    IdentityFile ~/.ssh/koan_id_ed25519
    IdentitiesOnly no
```

The `IdentitiesOnly no` setting ensures SSH tries the agent first, then falls
back to the `IdentityFile` if the agent is unavailable.

### Step 4: Verify

```bash
# Test with fallback key only (no agent)
SSH_AUTH_SOCK= ssh -T git@github.com

# Test with agent (should use forwarded key)
ssh -T git@github.com
```

### Step 5: Start Koan

```bash
ssh -A user@your-server
make start
```

When connected, git uses your forwarded agent. When disconnected, git
seamlessly falls back to the server-side key.

---

## Scenario 4: Docker — With SSH Agent Forwarding

`setup-docker.sh` auto-detects your SSH agent socket and mounts it into the
container.

### Linux host

```bash
# Ensure SSH agent is running
ssh-add -l

# Run setup (auto-detects SSH_AUTH_SOCK)
./setup-docker.sh

# Start container
make docker-up
```

The setup script adds a volume mount for your `SSH_AUTH_SOCK` socket file.
The container's entrypoint detects it and configures `SSH_AUTH_SOCK`
automatically.

### macOS / Docker Desktop

Docker Desktop for Mac has built-in SSH agent forwarding. Mount the magic
socket path:

Add to `docker-compose.override.yml` (or let `setup-docker.sh` handle it):

```yaml
services:
  koan:
    volumes:
      - /run/host-services/ssh-auth.sock:/run/ssh-agent.sock:ro
```

---

## Scenario 5: Docker — With SSH Key Mount

If agent forwarding isn't available, mount your SSH keys into the container.
`setup-docker.sh` auto-detects `~/.ssh` and mounts it read-only.

```bash
# Ensure ~/.ssh exists with your keys
ls ~/.ssh/id_*

# Run setup (auto-detects ~/.ssh)
./setup-docker.sh

# Start container
make docker-up
```

The container's entrypoint starts a local SSH agent and loads the mounted
keys. Only passphrase-less keys are loaded (no interactive prompt).

For a dedicated key, generate one as described in
[Generating a Fallback Key](#generating-a-fallback-key).

---

## Scenario 6: Docker — HTTPS Only (No SSH)

If you don't need SSH, use GitHub's HTTPS transport with a token. This is
already fully supported:

```bash
# Extract token from host gh CLI
make docker-gh-auth

# Start container
make docker-up
```

The `GH_TOKEN` environment variable enables `gh` CLI operations and can be
used for HTTPS git operations. No SSH setup needed.

**Limitation:** Repos must be accessible via the token. Private repos in orgs
where you don't have token access won't work.

---

## Generating a Fallback Key

This section is referenced by multiple scenarios above.

```bash
# Generate a dedicated key for Koan
ssh-keygen -t ed25519 -f ~/.ssh/koan_id_ed25519 -N "" -C "koan-fallback@$(hostname)"

# Add to GitHub: https://github.com/settings/keys
cat ~/.ssh/koan_id_ed25519.pub

# Configure SSH fallback
cat >> ~/.ssh/config << 'EOF'
Host github.com
    IdentityFile ~/.ssh/koan_id_ed25519
    IdentitiesOnly no
EOF

# Verify
SSH_AUTH_SOCK= ssh -T git@github.com
```

---

## Troubleshooting

### "Permission denied (publickey)"

1. **Check agent:** `ssh-add -l` — are keys loaded?
2. **Check key on GitHub:** Go to [github.com/settings/keys](https://github.com/settings/keys)
3. **Test SSH:** `ssh -vT git@github.com` (verbose mode shows which keys are tried)
4. **systemd:** Run `make ssh-forward` to refresh the agent socket
5. **Docker:** Check the container logs for SSH auth messages

### SSH agent socket not forwarded

```bash
# Verify SSH_AUTH_SOCK is set
echo $SSH_AUTH_SOCK

# Verify it points to a valid socket
ls -la $SSH_AUTH_SOCK

# Ensure you connected with -A
# Check your ~/.ssh/config has: ForwardAgent yes
```

### systemd service can't reach agent after reconnect

```bash
# Refresh the agent symlink
make ssh-forward

# Verify the symlink
ls -la .ssh-agent-sock

# No service restart needed
```

### Docker container has no SSH access

```bash
# Check what's mounted
docker compose exec koan ls -la /run/ssh-agent.sock /home/koan/.ssh/ 2>/dev/null

# Re-run setup to regenerate mounts
./setup-docker.sh
docker compose up --build -d
```
