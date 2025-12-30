# c3dl

CCC Media Downloader - Downloads relive streams and finalized releases from Chaos Communication Congress events.

## Installation

```bash
# Clone or download
git clone https://github.com/efnats/c3dl.git
cd c3dl

# Install dependencies
pip install -r requirements.txt

# Make executable (optional)
chmod +x c3dl.py
```

### System-wide installation (optional)

```bash
sudo cp c3dl.py /usr/local/bin/c3dl
sudo chmod +x /usr/local/bin/c3dl
```

## Usage

```bash
# Download 39c3 in HD (1080p) to ./39c3/
./c3dl.py -c 39c3

# Different quality presets
./c3dl.py -c 39c3 -q sd          # 576p (smaller files)
./c3dl.py -c 39c3 -q webm        # 1080p WebM
./c3dl.py -c 39c3 -q mp3         # Audio only
./c3dl.py -c 39c3 -q opus        # Audio only (smallest)

# Custom output directory
./c3dl.py -c 39c3 -o ~/Videos

# Run once and exit (no loop)
./c3dl.py -c 39c3 --once

# Only releases or only relive streams
./c3dl.py -c 39c3 --releases-only
./c3dl.py -c 39c3 --relive-only

# Longer wait time between checks (5 minutes)
./c3dl.py -c 39c3 -w 300
```

## Options

| Option | Description |
|--------|-------------|
| `-c, --congress ID` | Congress identifier (required), e.g., 38c3, 39c3 |
| `-o, --output-dir DIR` | Base output directory (default: current directory) |
| `-q, --quality PRESET` | Quality preset (default: hd) |
| `-w, --wait-time SEC` | Seconds between checks (default: `120`) |
| `-r, --retries N` | Number of retry attempts for failed downloads (default: `0`) |
| `--once` | Run once and exit |
| `--releases-only` | Only download finalized releases |
| `--relive-only` | Only download relive streams |
| `--dry-run` | Show what would be downloaded |
| `--no-color` | Disable colored output |
| `--no-cleanup` | Disable automatic cleanup (relives, duplicates) |
| `--clean-partial` | Remove all partial downloads and exit |

## Quality Presets

| Preset | Format | Resolution | Size per Talk |
|--------|--------|------------|---------------|
| `hd` | MP4 | 1080p | ~1-3 GB |
| `sd` | MP4 | 576p | ~200-400 MB |
| `webm` | WebM | 1080p | ~500 MB |
| `webm-sd` | WebM | 576p | ~100-200 MB |
| `mp3` | MP3 | Audio | ~30-50 MB |
| `opus` | Opus | Audio | ~15-30 MB |

## Output Structure

```
{output-dir}/
└── {congress}/
    ├── relive/      # Live recordings (available during/shortly after congress)
    │   ├── Talk_Title_1.mp4
    │   └── Talk_Title_2.mp4
    └── releases/    # Finalized releases (post-processed, higher quality)
        ├── talk_title_1.mp4
        └── talk_title_2.mp4
```

## Running as systemd service

Create `/etc/systemd/system/c3dl.service`:

```ini
[Unit]
Description=c3dl - CCC Media Downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=media
ExecStart=/usr/local/bin/c3dl -c 39c3 -o /srv/media/ccc
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now c3dl
sudo journalctl -fu c3dl
```

## Notes

- **Releases** are fetched from the podcast feed at `media.ccc.de/c/{congress}/podcast/{format}.xml`
- **Relive streams** are fetched from `streaming.media.ccc.de/{congress}/relive`
- Relive streams are available during and shortly after the congress (faster, but only MP4)
- Releases are post-processed and uploaded over the following weeks/months (multiple quality options)
- **Smart deduplication**: Relive downloads are skipped if a release already exists (fuzzy title matching)
- **Auto-cleanup**: Relive files are automatically removed when their release becomes available
- **Duplicate detection**: Duplicates are detected and removed at the start of each cycle (keeps the longer/more complete filename)
- **Smart renaming**: If a title changes upstream, existing files are renamed instead of re-downloaded
- Quality setting (`-q`) only applies to releases, not relive streams
- The script uses file locking to prevent multiple instances for the same congress
- **Resume support**: Interrupted downloads are saved as `.part` files and automatically resumed
- **Retry support**: Use `-r N` to retry failed downloads N times with exponential backoff
- File sizes are verified against the feed – incomplete files are re-downloaded automatically
- Colors are disabled automatically when output is piped (or use `--no-color`)
- Use `--clean-partial` to remove all `.part` files and start fresh
- Use `--no-cleanup` to disable automatic cleanup of relives and duplicates

## License

MIT
