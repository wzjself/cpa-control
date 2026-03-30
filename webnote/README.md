# WebNote

A lightweight OneNote-like web notebook built with Python + Flask.

## Features

- Multi-tab note pages
- Rich text editing in the browser
- Paste images directly into notes
- Document upload to a dedicated server folder
- SQLite-backed sync across devices via the same server URL
- Responsive UI for desktop and mobile

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Default port: `8320`

Uploaded files are stored under:

- `uploads/docs/`
- `uploads/images/`

Database:

- `data/notes.db`
