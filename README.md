# Catsync

**What it is.** Catsync keeps a local folder in sync with Google Drive on **Omarchy** (Arch Linux + Hyprland). Use it instead of Insync/Dropbox-style clients: bidirectional upload/download/move, conflicts kept as copies, deletes go to trash. It runs as a systemd user daemon, with an optional GTK tray.

The Omarchy bar plugin is a separate repository: [cytracon/omarchy-catsync](https://github.com/cytracon/omarchy-catsync).

## Privacy

OAuth client JSON, token, and `config.json` live only in **`~/.config/catsyncd/`** (mode 600). They are never part of this repository. `data/config.json.example` is a generic template with no account data.

## Install on Omarchy

```bash
omarchy install catsync
```

or from a checkout:

```bash
./install-local.sh
```

Existing `~/.config/catsyncd/` is kept. The installer does not write credentials.

## Setup (first machine)

1. Create a Google Cloud **Desktop** OAuth client.
2. Import it:

```bash
catsyncd import-oauth-client ~/Downloads/client_secret_*.json
catsyncd auth
catsyncd doctor
```

3. Enable writes in `~/.config/catsyncd/config.json` when you are ready (`allow_writes`, `dry_run`).

## CLI

```bash
catsyncd --version
catsyncd --status          # JSON for the bar plugin; no secrets or paths
catsyncd doctor
catsyncd plan
catsyncd daemon
catsync-tray --settings
```

## Plugin

```bash
omarchy plugin add https://github.com/cytracon/omarchy-catsync.git --enable
```

## License

MIT.
