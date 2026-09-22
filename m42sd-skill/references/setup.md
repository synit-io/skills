# Setup and configuration

`<skill-dir>` is the directory containing `SKILL.md`.

## Credentials

Use a dedicated Matrix42 Person with least privilege. Generate its API token in
Administration > Integration > Web Service Tokens. Never use a shared admin token.

The human provides the token, never the agent, and never on the command line:

- Interactive: run `setup` in a terminal and paste the token at the prompt.
- Non-interactive: `export M42_API_TOKEN=...` in the shell that runs `setup`.
- `--token` still works but is deprecated: it puts the secret into the process
  list and shell history. The CLI prints a warning on stderr.

The config stores the API token as plaintext with mode 0600. Keep the file out
of version control, never print or paste it, and revoke the token immediately
if exposed. `whoami` reports token validity and expiry.

## Config file location

One function resolves the path for reads and writes, in this order:

1. `M42_CONFIG_PATH`, when set.
2. `<skill-dir>/scripts/m42_config.json`, when that legacy file already exists
   (pre-existing installations keep working unchanged).
3. `$XDG_CONFIG_HOME/m42sd/m42_config.json`, falling back to
   `~/.config/m42sd/m42_config.json`. `setup` creates the directory with mode
   0700 and writes the file atomically (temp file with 0600, then rename).

`load_client` refuses a config file that is group- or world-accessible and
tells the user to `chmod 600` it.

Environment overrides: `M42_BASE_URL` and `M42_API_TOKEN` override stored
credentials. `M42_TENANT_PROFILE_FILE` may override stored behavior only with a
separately human-reviewed profile. When the environment URL selects another
tenant, provide matching token and profile together; stored tenant behavior is
not reused. HTTPS is mandatory except for loopback development, and the client
refuses any redirect that leaves the configured HTTPS origin.

## Two-pass setup

First run discovery without `--profile-file`:

```bash
python3 <skill-dir>/scripts/m42.py setup --base-url https://<tenant-host>
```

This pass reads available states and state groups, urgency, impact, close
reasons, journal templates, forward roles, and ticket prefixes. It prints JSON
containing the live inventory, setup questions, and a profile template; it
writes no config. Some sections can be unavailable when the token lacks read
access. Do not infer missing choices from examples or another tenant.

Ask the human every emitted setup question. In particular, confirm semantic
state mappings, allowed close reasons and roles, ticket families, pre-close
paths, state-close fallback permission, automatic responsible-person
assignment, language mode, close questions, and optional portal URL.
`journal_actions` may use `null`; that selects a plain internal audit entry
instead of an unverified native template. `journal_actions.state_change` is
optional and covers state changes that have no dedicated action. For
unsupported ticket families, explicitly set their prefix to `null`;
family-dependent operations will stop for those prefixes. Discovery is a capped
sample: review `possibly_truncated` and add known prefixes it missed.

Write answers to a temporary profile based on
`references/tenant-profile.example.json`, replacing every placeholder. The
validator rejects any `<...>` placeholder marker and any `example.com`-style
host in `portal_url_template`, so an unedited example never validates. Then run:

```bash
python3 <skill-dir>/scripts/m42.py setup \
  --base-url https://<tenant-host> \
  --profile-file /path/to/reviewed-tenant-profile.json
```

Setup repeats live discovery, rejects selected pickup values missing from
readable live inventories, then stores credentials and reviewed tenant behavior
together in the config file. Run `tenant-config` after setup and before the
first write in a session; its output is non-secret and is authoritative for
agent behavior.
