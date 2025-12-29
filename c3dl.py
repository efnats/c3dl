#!/usr/bin/env python3
"""
c3dl - CCC Media Downloader

Downloads relive streams and finalized releases from Chaos Communication Congress events.

Usage:
    c3dl.py -c 39c3                   # Download HD (1080p) to ./39c3/
    c3dl.py -c 39c3 -q sd             # Download SD (576p, smaller files)
    c3dl.py -c 39c3 -q opus           # Download audio only (smallest)
    c3dl.py -c 38c3 -o ~/videos       # Custom output directory
    c3dl.py -c 39c3 --once            # Run once, don't loop
    c3dl.py -c 39c3 --releases-only   # Only download releases
"""

import argparse
import difflib
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from filelock import FileLock
from tqdm import tqdm


class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'

    @classmethod
    def disable(cls):
        """Disable colors (e.g., for non-TTY output)"""
        cls.HEADER = cls.BLUE = cls.CYAN = cls.GREEN = ''
        cls.YELLOW = cls.RED = cls.BOLD = cls.DIM = cls.RESET = ''


# Disable colors if not a TTY
if not sys.stdout.isatty():
    Colors.disable()


@dataclass
class Config:
    """Configuration container with dynamic URL generation"""
    congress: str
    base_dir: Path
    quality: str = "hd"
    wait_time: int = 900

    # Quality presets: (feed_name, file_extension, description)
    QUALITY_PRESETS = {
        "hd":    ("mp4-hq", ".mp4",  "1080p MP4"),
        "sd":    ("mp4",    ".mp4",  "576p MP4"),
        "webm":  ("webm-hq", ".webm", "1080p WebM"),
        "webm-sd": ("webm", ".webm", "576p WebM"),
        "mp3":   ("mp3",    ".mp3",  "MP3 audio"),
        "opus":  ("opus",   ".opus", "Opus audio"),
    }

    @property
    def feed_name(self) -> str:
        return self.QUALITY_PRESETS[self.quality][0]

    @property
    def file_extension(self) -> str:
        return self.QUALITY_PRESETS[self.quality][1]

    @property
    def quality_description(self) -> str:
        return self.QUALITY_PRESETS[self.quality][2]

    @property
    def relive_base_url(self) -> str:
        return f"https://streaming.media.ccc.de/{self.congress}/relive"

    @property
    def relive_cdn_base(self) -> str:
        return f"https://cdn.c3voc.de/relive/{self.congress}"

    @property
    def releases_rss_url(self) -> str:
        return f"https://media.ccc.de/c/{self.congress}/podcast/{self.feed_name}.xml"

    @property
    def relive_dir(self) -> Path:
        return self.base_dir / self.congress / "relive"

    @property
    def releases_dir(self) -> Path:
        return self.base_dir / self.congress / "releases"

    @property
    def lock_file(self) -> Path:
        return Path(f"/tmp/{self.congress}_downloader.lock")

    def ensure_directories(self):
        """Create output directories if they don't exist"""
        self.relive_dir.mkdir(parents=True, exist_ok=True)
        self.releases_dir.mkdir(parents=True, exist_ok=True)


def sanitize_filename(title: str, extension: str, max_bytes: int = 240) -> str:
    """
    Create a safe filename from a title.
    
    - Replaces invalid characters
    - Truncates to max_bytes (accounting for UTF-8 encoding)
    - Leaves room for extension
    """
    # Replace invalid filesystem characters
    filename = re.sub(r'[\/:*?"<>|]', '_', title)
    
    # Calculate available bytes for the name (excluding extension)
    available_bytes = max_bytes - len(extension.encode('utf-8'))
    
    # Truncate to fit byte limit while keeping valid UTF-8
    encoded = filename.encode('utf-8')
    if len(encoded) > available_bytes:
        # Truncate and decode, ignoring incomplete multi-byte chars
        truncated = encoded[:available_bytes - 3]  # Room for "..."
        # Find last valid UTF-8 boundary
        while truncated and (truncated[-1] & 0xC0) == 0x80:
            truncated = truncated[:-1]
        filename = truncated.decode('utf-8', errors='ignore') + "..."
    
    return filename + extension


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string"""
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes} B"


def normalize_title(filename: str) -> str:
    """
    Normalize a filename for comparison.
    
    Removes:
    - File extension
    - Congress tags like (39c3), (38c3)
    - Common suffixes/prefixes
    - Extra whitespace and punctuation
    """
    # Remove extension
    name = Path(filename).stem
    
    # Remove congress tags like (39c3), (38c3), etc.
    name = re.sub(r'\s*\(\d{2}c\d\)\s*', '', name, flags=re.I)
    
    # Remove common separators and normalize
    name = re.sub(r'[_\-–—]', ' ', name)
    
    # Remove special characters but keep umlauts
    name = re.sub(r'[^\w\s\u00C0-\u017F]', '', name)
    
    # Normalize whitespace
    name = ' '.join(name.split())
    
    return name.lower().strip()


def find_matching_release(relive_title: str, releases_dir: Path, threshold: float = 0.85) -> Optional[Path]:
    """
    Find a release file that matches a relive title.
    
    Uses fuzzy matching to handle slight differences in naming.
    Returns the matching release path or None.
    """
    if not releases_dir.exists():
        return None
    
    normalized_relive = normalize_title(relive_title)
    
    for release_file in releases_dir.glob("*"):
        if not release_file.is_file():
            continue
        if release_file.suffix.lower() not in ('.mp4', '.webm', '.mp3', '.opus'):
            continue
            
        normalized_release = normalize_title(release_file.name)
        
        # Calculate similarity ratio
        ratio = difflib.SequenceMatcher(None, normalized_relive, normalized_release).ratio()
        
        if ratio >= threshold:
            return release_file
    
    return None


def cleanup_relive_duplicates(config: Config) -> int:
    """
    Remove relive files that have a corresponding release.
    
    Returns number of files removed.
    """
    if not config.relive_dir.exists() or not config.releases_dir.exists():
        return 0
    
    removed = 0
    
    for relive_file in list(config.relive_dir.glob("*.mp4")):
        matching_release = find_matching_release(relive_file.name, config.releases_dir)
        
        if matching_release:
            relive_size = relive_file.stat().st_size
            print(f"  {Colors.DIM}Removing relive (release exists): {relive_file.name}{Colors.RESET}")
            print(f"  {Colors.DIM}  → Matched: {matching_release.name}{Colors.RESET}")
            relive_file.unlink()
            removed += 1
    
    return removed


def get_terminal_width() -> int:
    """Get terminal width, default to 80 if not available"""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def truncate_for_display(text: str, max_width: int = 50) -> str:
    """Truncate text for display, keeping it readable"""
    if len(text) <= max_width:
        return text
    return text[:max_width - 3] + "..."


def download_file(url: str, output_path: Path, description: str, expected_size: int = 0, max_retries: int = 3) -> bool:
    """
    Download a file with progress bar, resume support, and retry logic.
    
    Downloads to a .part file first, then renames on success.
    Supports resuming partial downloads if server supports Range requests.
    Returns True on success, False on failure.
    """
    part_path = output_path.with_suffix(output_path.suffix + '.part')
    
    for attempt in range(max_retries):
        try:
            # Check if we can resume a partial download
            resume_pos = 0
            headers = {}
            mode = 'wb'
            
            if part_path.exists():
                resume_pos = part_path.stat().st_size
                headers['Range'] = f'bytes={resume_pos}-'
                mode = 'ab'  # Append mode
            
            with requests.get(url, stream=True, timeout=30, headers=headers) as r:
                # Check if server supports resume (206 Partial Content)
                if r.status_code == 206:
                    # Resuming - get total size from Content-Range header
                    content_range = r.headers.get('Content-Range', '')
                    if '/' in content_range:
                        total_size = int(content_range.split('/')[-1])
                    else:
                        total_size = resume_pos + int(r.headers.get('content-length', 0))
                    
                    if attempt == 0 and resume_pos > 0:
                        print(f"{Colors.CYAN}↻ Resuming from {format_size(resume_pos)}{Colors.RESET}")
                elif r.status_code == 200:
                    # Server doesn't support resume or fresh download
                    total_size = int(r.headers.get('content-length', 0))
                    if resume_pos > 0:
                        # Server sent full file, start over
                        resume_pos = 0
                        mode = 'wb'
                else:
                    r.raise_for_status()
                    total_size = 0

                # Calculate display width for progress bar description
                term_width = get_terminal_width()
                # Leave room for: progress bar (~30), percentage (~7), size (~20), speed (~15)
                desc_width = max(20, term_width - 75)
                short_desc = truncate_for_display(description, desc_width)

                with open(part_path, mode) as f, tqdm(
                    desc=short_desc,
                    total=total_size,
                    initial=resume_pos,
                    unit='iB',
                    unit_scale=True,
                    unit_divisor=1024,
                    bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]',
                    ncols=term_width
                ) as pbar:
                    for chunk in r.iter_content(chunk_size=8192):
                        size = f.write(chunk)
                        pbar.update(size)

            # Verify downloaded size
            actual_size = part_path.stat().st_size
            check_size = expected_size if expected_size > 0 else total_size
            
            if check_size > 0 and actual_size < check_size * 0.99:
                print(f"{Colors.RED}✗ Size mismatch: {format_size(actual_size)} / {format_size(check_size)}{Colors.RESET}")
                # Don't delete part file - might be able to resume
                continue  # Retry

            # Rename .part to final filename on success
            part_path.rename(output_path)
            print(f"{Colors.GREEN}✓ Downloaded{Colors.RESET}")
            return True

        except Exception as e:
            retry_msg = f" (attempt {attempt + 1}/{max_retries})" if attempt < max_retries - 1 else ""
            print(f"{Colors.RED}✗ Failed{retry_msg}: {e}{Colors.RESET}")
            
            if attempt < max_retries - 1:
                # Wait before retry with exponential backoff
                wait_time = 2 ** attempt * 5  # 5s, 10s, 20s
                print(f"{Colors.YELLOW}  Retrying in {wait_time}s...{Colors.RESET}")
                time.sleep(wait_time)
    
    # All retries failed - keep .part file for future resume
    print(f"{Colors.DIM}  Partial download kept for future resume{Colors.RESET}")
    return False


def download_releases(config: Config) -> int:
    """
    Download finalized releases from media.ccc.de podcast feed
    
    Returns number of newly downloaded files.
    """
    print(f"{Colors.BOLD}Fetching {config.congress} releases...{Colors.RESET}")
    downloaded = 0

    try:
        response = requests.get(config.releases_rss_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml')

        # Find all items in the podcast feed
        items = soup.find_all('item')

        if not items:
            print(f"{Colors.YELLOW}No releases found for {config.congress}{Colors.RESET}")
            return 0

        # Build list of downloads with size info
        downloads = []
        already_have = 0
        incomplete = 0

        for item in items:
            title_tag = item.find('title')
            enclosure = item.find('enclosure')

            if not title_tag or not enclosure:
                continue

            title = title_tag.text.strip()
            url = enclosure.get('url')
            size = int(enclosure.get('length', 0))

            if not url:
                continue

            # Create safe filename from title (handles byte limits for UTF-8)
            filename = sanitize_filename(title, config.file_extension)
            output_path = config.releases_dir / filename

            if output_path.exists():
                # Check file size matches expected size
                actual_size = output_path.stat().st_size
                if size > 0 and actual_size < size * 0.99:  # Allow 1% tolerance
                    # File is incomplete, delete and re-download
                    print(f"{Colors.YELLOW}⚠ Incomplete: {filename} ({format_size(actual_size)} / {format_size(size)}){Colors.RESET}")
                    output_path.unlink()
                    incomplete += 1
                else:
                    already_have += 1
                    continue
            
            # Check for existing .part file (can be resumed)
            part_path = output_path.with_suffix(output_path.suffix + '.part')
            resumable = part_path.exists()

            downloads.append({
                'title': title,
                'url': url,
                'filename': filename,
                'output_path': output_path,
                'size': size,
                'resumable': resumable,
            })

        # Print summary
        total_items = already_have + len(downloads)
        total_size = sum(d['size'] for d in downloads)
        resumable_count = sum(1 for d in downloads if d['resumable'])

        print(f"Found {Colors.BOLD}{total_items}{Colors.RESET} release(s)")
        print(f"  {Colors.GREEN}✓ Complete:{Colors.RESET} {already_have}")
        if incomplete:
            print(f"  {Colors.YELLOW}⚠ Incomplete:{Colors.RESET} {incomplete} (will re-download)")
        remaining_msg = f"{len(downloads)} file(s) ({Colors.YELLOW}{format_size(total_size)}{Colors.RESET})"
        if resumable_count:
            remaining_msg += f" {Colors.CYAN}({resumable_count} resumable){Colors.RESET}"
        print(f"  {Colors.CYAN}↓ Remaining:{Colors.RESET} {remaining_msg}")

        if not downloads:
            return 0

        # Download missing files
        for i, dl in enumerate(downloads, 1):
            print(f"\n{Colors.BOLD}[{i}/{len(downloads)}]{Colors.RESET} {dl['title']}")

            if download_file(dl['url'], dl['output_path'], dl['filename'], dl['size']):
                downloaded += 1

    except Exception as e:
        print(f"{Colors.RED}Error fetching releases: {e}{Colors.RESET}")

    return downloaded


def download_relive(config: Config) -> int:
    """
    Download relive streams from streaming.media.ccc.de
    
    Skips relive streams that already exist as releases.
    Returns number of newly downloaded files.
    """
    print(f"{Colors.BOLD}Fetching {config.congress} relive streams...{Colors.RESET}")
    downloaded = 0

    try:
        response = requests.get(config.relive_base_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract relive IDs from links
        relive_ids = set()
        for link in soup.find_all('a', href=True):
            if '/relive/' in link['href']:
                relive_id = link['href'].split('/')[-1]
                if relive_id.isdigit():
                    relive_ids.add(relive_id)

        if not relive_ids:
            print(f"{Colors.YELLOW}No relive streams found for {config.congress}{Colors.RESET}")
            return 0

        # Build list of downloads
        downloads = []
        already_have = 0
        has_release = 0

        for relive_id in sorted(relive_ids):
            title = get_relive_title(config, relive_id)
            if not title:
                continue

            filename = sanitize_filename(title, ".mp4")
            output_path = config.relive_dir / filename

            if output_path.exists():
                already_have += 1
                continue

            # Check if release already exists for this talk
            if find_matching_release(title, config.releases_dir):
                has_release += 1
                continue

            downloads.append({
                'relive_id': relive_id,
                'title': title,
                'filename': filename,
                'output_path': output_path,
            })

        # Print summary
        total = already_have + has_release + len(downloads)
        print(f"Found {Colors.BOLD}{total}{Colors.RESET} relive stream(s)")
        print(f"  {Colors.GREEN}✓ Already downloaded:{Colors.RESET} {already_have}")
        if has_release:
            print(f"  {Colors.BLUE}✓ Release exists:{Colors.RESET} {has_release}")
        print(f"  {Colors.CYAN}↓ Remaining:{Colors.RESET} {len(downloads)} file(s)")

        if not downloads:
            return 0

        # Download missing files
        for i, dl in enumerate(downloads, 1):
            print(f"\n{Colors.BOLD}[{i}/{len(downloads)}] Relive #{dl['relive_id']}:{Colors.RESET} {dl['title']}")
            
            video_url = f"{config.relive_cdn_base}/{dl['relive_id']}/muxed.mp4"
            if download_file(video_url, dl['output_path'], dl['filename']):
                downloaded += 1

    except Exception as e:
        print(f"{Colors.RED}Error fetching relive streams: {e}{Colors.RESET}")

    return downloaded


def get_relive_title(config: Config, relive_id: str) -> Optional[str]:
    """Fetch and parse the title for a relive stream"""
    try:
        url = f"{config.relive_base_url}/{relive_id}"
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.title.string if soup.title else None

        if not title or "Relive:" not in title:
            return None

        # Clean up: "Relive: Talk Title — 39C3" -> "Talk Title"
        # Split on various dash types (em-dash, en-dash, regular)
        for separator in [" — ", " – ", " - "]:
            if separator in title:
                title = title.split(separator)[0]
                break
        title = title.replace("Relive: ", "")

        return title.strip()

    except Exception:
        return None


def cleanup_partial_downloads(config: Config) -> int:
    """Remove any leftover .part files from previous runs"""
    count = 0
    for directory in [config.relive_dir, config.releases_dir]:
        if directory.exists():
            for part_file in directory.glob("*.part"):
                size = part_file.stat().st_size
                print(f"  {Colors.DIM}Removing: {part_file.name} ({format_size(size)}){Colors.RESET}")
                part_file.unlink()
                count += 1
    return count


def count_partial_downloads(config: Config) -> int:
    """Count .part files that can be resumed"""
    count = 0
    for directory in [config.relive_dir, config.releases_dir]:
        if directory.exists():
            count += len(list(directory.glob("*.part")))
    return count


def print_stats(config: Config):
    """Print download statistics for both directories"""
    title = f" Statistics for {config.congress} "
    width = max(50, len(title) + 4)
    
    print()
    print(f"{Colors.BOLD}╭{'─' * (width - 2)}╮{Colors.RESET}")
    print(f"{Colors.BOLD}│{title:^{width - 2}}│{Colors.RESET}")
    print(f"{Colors.BOLD}╰{'─' * (width - 2)}╯{Colors.RESET}")

    media_extensions = ("*.mp4", "*.webm", "*.mp3", "*.opus")

    for name, directory in [("Relive", config.relive_dir), ("Releases", config.releases_dir)]:
        if directory.exists():
            files = []
            for ext in media_extensions:
                files.extend(directory.glob(ext))
            total_size = sum(f.stat().st_size for f in files)
            print(f"\n{Colors.CYAN}{name}:{Colors.RESET}")
            print(f"  Files: {Colors.BOLD}{len(files)}{Colors.RESET}")
            print(f"  Size:  {Colors.BOLD}{format_size(total_size)}{Colors.RESET}")
            print(f"  {Colors.DIM}Path:  {directory}{Colors.RESET}")
        else:
            print(f"\n{Colors.DIM}{name}: (directory not created yet){Colors.RESET}")

    print()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Download CCC congress media (relive streams and releases)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -c 39c3                    Download 39c3 in HD (1080p) to ./39c3/
  %(prog)s -c 39c3 -q sd              Download in SD (576p, ~5x smaller)
  %(prog)s -c 39c3 -q opus            Download audio only (smallest)
  %(prog)s -c 38c3 -o ~/videos        Download 38c3 to ~/videos/38c3/
  %(prog)s -c 39c3 --once             Run once and exit
  %(prog)s -c 39c3 --releases-only    Skip relive streams

Quality presets:
  hd       1080p MP4 (default, ~1-3 GB/talk)
  sd       576p MP4 (~200-400 MB/talk)
  webm     1080p WebM (~500 MB/talk)
  webm-sd  576p WebM (~100-200 MB/talk)
  mp3      MP3 audio (~30-50 MB/talk)
  opus     Opus audio (~15-30 MB/talk)

Note: Quality setting only applies to releases. Relive streams are always MP4.
        """
    )

    parser.add_argument(
        "-c", "--congress",
        required=True,
        metavar="ID",
        help="Congress identifier, e.g., 38c3, 39c3"
    )

    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Base output directory (default: current directory)"
    )

    parser.add_argument(
        "-q", "--quality",
        choices=["hd", "sd", "webm", "webm-sd", "mp3", "opus"],
        default="hd",
        help="Quality preset: hd (1080p MP4), sd (576p MP4), webm (1080p), webm-sd (576p), mp3, opus (default: hd)"
    )

    parser.add_argument(
        "-w", "--wait-time",
        type=int,
        default=900,
        metavar="SEC",
        help="Seconds between checks in loop mode (default: 900)"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)"
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--releases-only",
        action="store_true",
        help="Only download releases (skip relive)"
    )
    mode_group.add_argument(
        "--relive-only",
        action="store_true",
        help="Only download relive streams (skip releases)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading"
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )

    parser.add_argument(
        "--clean-partial",
        action="store_true",
        help="Remove all partial (.part) downloads and exit"
    )

    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Don't remove relive files when releases become available"
    )

    return parser.parse_args()


def run_download_cycle(config: Config, args: argparse.Namespace) -> int:
    """
    Run one download cycle.
    
    Returns total number of new downloads.
    """
    total = 0

    if not args.releases_only:
        total += download_relive(config)

    if not args.relive_only:
        total += download_releases(config)
        
        # Clean up relive files that now have releases (unless disabled)
        if not args.no_cleanup:
            cleaned = cleanup_relive_duplicates(config)
            if cleaned:
                print(f"{Colors.GREEN}Cleaned up {cleaned} relive duplicate(s){Colors.RESET}")

    return total


def main():
    args = parse_args()

    # Handle --no-color
    if args.no_color:
        Colors.disable()

    config = Config(
        congress=args.congress.lower(),
        base_dir=args.output_dir,
        quality=args.quality,
        wait_time=args.wait_time,
    )

    print(f"{Colors.BOLD}{Colors.CYAN}c3dl{Colors.RESET} - CCC Media Downloader")
    print(f"{Colors.BOLD}Congress:{Colors.RESET}   {config.congress}")
    print(f"{Colors.BOLD}Quality:{Colors.RESET}    {config.quality_description}")
    print(f"{Colors.BOLD}Output:{Colors.RESET}     {config.base_dir}")
    print(f"{Colors.DIM}Relive:     {config.relive_dir}{Colors.RESET}")
    print(f"{Colors.DIM}Releases:   {config.releases_dir}{Colors.RESET}")
    print(f"{Colors.DIM}Feed:       {config.releases_rss_url}{Colors.RESET}")
    print()

    if args.dry_run:
        print(f"{Colors.YELLOW}DRY RUN - no files will be downloaded{Colors.RESET}")
        print()

    if args.relive_only:
        print(f"{Colors.YELLOW}Note: Quality setting is ignored for relive streams (only MP4 available){Colors.RESET}")
        print()

    # Use file lock to prevent multiple instances for same congress
    lock = FileLock(str(config.lock_file), timeout=1)

    try:
        lock.acquire()
    except TimeoutError:
        print(f"{Colors.RED}Error: Another instance is already downloading {config.congress}{Colors.RESET}")
        sys.exit(1)

    try:
        config.ensure_directories()
        
        # Handle --clean-partial
        if args.clean_partial:
            print(f"{Colors.YELLOW}Cleaning up partial downloads...{Colors.RESET}")
            cleaned = cleanup_partial_downloads(config)
            if cleaned:
                print(f"{Colors.GREEN}Removed {cleaned} partial download(s){Colors.RESET}")
            else:
                print(f"{Colors.DIM}No partial downloads found{Colors.RESET}")
            return
        
        # Show count of resumable downloads
        partial_count = count_partial_downloads(config)
        if partial_count:
            print(f"{Colors.CYAN}Found {partial_count} partial download(s) that can be resumed{Colors.RESET}")
            print()

        if args.once:
            # Single run mode
            run_download_cycle(config, args)
            print_stats(config)
        else:
            # Loop mode
            print(f"{Colors.DIM}Running in loop mode (Ctrl+C to stop){Colors.RESET}")
            print()

            while True:
                try:
                    run_download_cycle(config, args)
                    print_stats(config)

                    print(f"{Colors.DIM}Waiting {config.wait_time} seconds...{Colors.RESET}")
                    for remaining in range(config.wait_time, 0, -1):
                        print(f"\r{Colors.DIM}Next check in {remaining:4d}s (Ctrl+C to stop){Colors.RESET}", end="")
                        time.sleep(1)
                    print("\n")

                except KeyboardInterrupt:
                    print(f"\n\n{Colors.YELLOW}Shutting down...{Colors.RESET}")
                    break

    finally:
        lock.release()
        print(f"{Colors.GREEN}Done.{Colors.RESET}")


if __name__ == "__main__":
    main()
