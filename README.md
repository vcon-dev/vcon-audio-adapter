# vCon Audio Adapter

A Python adapter that monitors a directory for audio files, extracts phone numbers from filenames, creates vCon (virtual conversation) objects, and posts them to a conserver endpoint.

## Features

- **Directory Monitoring**: Watches a directory for new audio files using `watchdog`
- **Filename Parsing**: Extracts sender and receiver phone numbers from configurable filename patterns
- **vCon Creation**: Creates vCon objects with proper dialog entries for audio recordings
- **Audio Duration**: Optionally extracts audio duration using `mutagen` or `ffprobe`
- **Conserver Integration**: Posts vCons to HTTP endpoints with configurable authentication
- **State Tracking**: Tracks processed files to prevent duplicates
- **Ingress Routing**: Supports routing vCons to specific processing queues

## Installation

### Using pip

```bash
pip install vcon-audio-adapter
```

### With optional dependencies

```bash
# Include audio duration extraction support
pip install vcon-audio-adapter[duration]

# Include S3 support
pip install vcon-audio-adapter[s3]

# Include all optional dependencies
pip install vcon-audio-adapter[all]
```

### From source

```bash
git clone https://github.com/vcon-dev/vcon-audio-adapter.git
cd vcon-audio-adapter
pip install -e .
```

## Quick Start

1. **Copy the example environment file:**

```bash
cp .env.example .env
```

2. **Edit `.env` with your configuration:**

```bash
# Required settings
WATCH_DIRECTORY=/path/to/audio/files
CONSERVER_URL=https://your-conserver.example.com/vcon
CONSERVER_API_TOKEN=your-api-token

# Optional: Configure ingress routing
INGRESS_LISTS=transcribe,analyze
```

3. **Run the adapter:**

```bash
python main.py
```

Or if installed as a package:

```bash
vcon-audio-adapter
```

## Filename Convention

By default, audio files should be named with the pattern:

```
{sender}_{receiver}.{extension}
```

Examples:
- `15085551212_19995551234.wav`
- `18005551234_12125551234.mp3`

The sender and receiver are extracted as phone numbers and added to the vCon as parties with `tel:` URIs.

### Custom Filename Patterns

You can customize the filename pattern using a regex with capture groups:

```bash
# Default pattern
FILENAME_PATTERN=(\d+)_(\d+)\.(wav|mp3|ogg|m4a|flac|aac|wma|aiff|opus)

# Custom pattern with date prefix: 20240115_sender_receiver.wav
FILENAME_PATTERN=\d{8}_(\d+)_(\d+)\.(wav|mp3)

# Pattern with call ID: callid-sender-receiver.wav
FILENAME_PATTERN=\w+-(\d+)-(\d+)\.(wav|mp3|ogg)
```

The pattern must have at least 2 capture groups:
1. First group: sender phone number
2. Second group: receiver phone number
3. Optional third group: file extension

## Configuration Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `SOURCE_TYPE` | Source type: `filesystem` or `s3` | `filesystem` |
| `WATCH_DIRECTORY` | Directory to monitor for audio files | Required |
| `CONSERVER_URL` | URL to POST vCons to | Required |
| `CONSERVER_API_TOKEN` | API token for authentication | None |
| `CONSERVER_HEADER_NAME` | Header name for API token | `x-conserver-api-token` |
| `FILENAME_PATTERN` | Regex pattern for parsing filenames | See above |
| `SUPPORTED_FORMATS` | Comma-separated list of extensions | `wav,mp3,ogg,m4a,flac,aac,wma,aiff,opus` |
| `DIALOG_TYPE` | vCon dialog type | `recording` |
| `EXTRACT_DURATION` | Extract audio duration | `true` |
| `INGRESS_LISTS` | Comma-separated ingress queues | None |
| `PROCESS_EXISTING` | Process existing files on startup | `true` |
| `DELETE_AFTER_SEND` | Delete files after successful upload | `false` |
| `STATE_FILE` | Path to state tracking file | `.audio_adapter_state.json` |
| `POLL_INTERVAL` | File system polling interval (seconds) | `1.0` |
| `AUDIO_URL_BASE` | HTTP(S) prefix used in the dialog `url` instead of `file://`. When set, each vCon dialog references `{AUDIO_URL_BASE}/<path-relative-to-WATCH_DIRECTORY>` (or `BASE_DIRECTORY` for iterator mode). Use this when the adapter and the conserver run in different containers/pods and need the audio fetched over HTTP. **Unset (default) preserves the legacy `file://` behaviour byte-for-byte.** | unset |

## vCon Structure

The adapter creates vCons with the following structure:

```json
{
  "vcon": "0.0.1",
  "uuid": "generated-uuid",
  "created_at": "2024-01-15T10:30:00Z",
  "parties": [
    {"tel": "15085551212"},
    {"tel": "19995551234"}
  ],
  "dialog": [
    {
      "type": "recording",
      "start": "2024-01-15T10:30:00Z",
      "parties": [0, 1],
      "originator": 0,
      "mimetype": "audio/wav",
      "filename": "15085551212_19995551234.wav",
      "body": "base64-encoded-audio-data",
      "encoding": "base64",
      "duration": 125.5
    }
  ],
  "tags": {
    "source": "audio_adapter",
    "original_filename": "15085551212_19995551234.wav",
    "sender": "15085551212",
    "receiver": "19995551234",
    "duration_seconds": "125.50"
  }
}
```

## Development

### Setup development environment

```bash
# Clone the repository
git clone https://github.com/vcon-dev/vcon-audio-adapter.git
cd vcon-audio-adapter

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install with development dependencies
pip install -e ".[dev]"
```

### Run tests

```bash
pytest
```

### Code formatting

```bash
black .
ruff check --fix .
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      AudioAdapter (main.py)                     │
│                   Orchestrates all components                   │
└─────────────────────────────────────────────────────────────────┘
         │              │              │              │
         ▼              ▼              ▼              ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│   Config    │ │   Parser    │ │   Builder   │ │   Poster    │
│ (config.py) │ │ (parser.py) │ │(builder.py) │ │ (poster.py) │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│              FileSystemMonitor (monitor.py)                     │
│                  Uses watchdog for events                       │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                  StateTracker (tracker.py)                      │
│               JSON-based duplicate prevention                   │
└─────────────────────────────────────────────────────────────────┘
```

## Related Projects

- [vcon-lib](https://github.com/vcon-dev/vcon-lib) - Python vCon library
- [vcon-fadapter](https://github.com/vcon-dev/vcon-fadapter) - Fax image vCon adapter (this project is based on)
- [vcon-server](https://github.com/vcon-dev/vcon-server) - vCon server implementation

## License

MIT License - see [LICENSE](LICENSE) for details.
