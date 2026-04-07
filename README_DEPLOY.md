# Deployment Notes

## WeasyPrint dependencies

Invoice PDF rendering uses WeasyPrint. On `python:3.11-slim` (Debian-based), install these runtime packages:

- `libcairo2`
- `libglib2.0-0`
- `libgdk-pixbuf-2.0-0`
- `libpango-1.0-0`
- `libpangoft2-1.0-0`
- `libharfbuzz0b`
- `libfribidi0`
- `libffi8`
- `shared-mime-info`
- `fonts-dejavu-core`

Example:

```bash
apt-get update && apt-get install -y --no-install-recommends \
  libcairo2 libglib2.0-0 libgdk-pixbuf-2.0-0 libpango-1.0-0 libpangoft2-1.0-0 \
  libharfbuzz0b libfribidi0 libffi8 shared-mime-info fonts-dejavu-core
```

Without these, invoice PDF download returns HTTP 500 (renderer unavailable).

### Windows local development

WeasyPrint on Windows requires GTK runtime DLLs (GLib/Pango/Cairo). A working option is MSYS2:

1. Install MSYS2.
2. Install runtime packages in `ucrt64`:

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-gtk3 mingw-w64-ucrt-x86_64-pango mingw-w64-ucrt-x86_64-gdk-pixbuf2 mingw-w64-ucrt-x86_64-harfbuzz mingw-w64-ucrt-x86_64-libffi
```

3. Ensure DLL discovery includes the runtime folder:

```powershell
set WEASYPRINT_DLL_DIRECTORIES=C:\msys64\ucrt64\bin
```

The app also probes common MSYS2 paths (`C:\msys64\ucrt64\bin`, `C:\msys64\mingw64\bin`) and prepends them to process `PATH` on Windows before running the WeasyPrint self-check.

## Public base URL for PDF assets

If invoice PDFs include images (for example `company.logo_url`), set:

```bash
APP_PUBLIC_BASE_URL=https://your-public-hostname
```

When invoice PDF rendering runs without a request context, this base URL is used so
WeasyPrint can resolve `/static/...` image paths.

## Persist Company Logo Uploads

Company logo uploads are written under `/data/uploads/tenants/<tenant_id>/company`. In Docker,
bind mount the upload root so tenant-separated files survive container rebuilds:

```yaml
services:
  web:
    volumes:
      - ./uploads:/data/uploads
```

Ensure the host folder exists before first run:

```bash
mkdir -p uploads
```

## Site Agent Download Link

To expose the Windows site-agent installer inside the SaaS settings and printing screens,
you can either set a fixed public download URL or configure a private Railway bucket for
on-demand presigned downloads.

Fixed URL mode:

```bash
SITE_AGENT_DOWNLOAD_URL=https://your-download-host/path/WeighbridgeSiteAgent-0.22-Setup.exe
SITE_AGENT_DOWNLOAD_VERSION=0.22
```

Private Railway bucket mode:

```bash
SITE_AGENT_DOWNLOAD_VERSION=0.22
SITE_AGENT_DOWNLOAD_BUCKET=<bucket-name>
SITE_AGENT_DOWNLOAD_OBJECT_KEY=downloads/WeighbridgeSiteAgent-0.22-Setup.exe
SITE_AGENT_DOWNLOAD_S3_ENDPOINT=<bucket-endpoint>
SITE_AGENT_DOWNLOAD_S3_REGION=<bucket-region>
SITE_AGENT_DOWNLOAD_ACCESS_KEY_ID=<bucket-access-key-id>
SITE_AGENT_DOWNLOAD_SECRET_ACCESS_KEY=<bucket-secret-access-key>
SITE_AGENT_DOWNLOAD_PRESIGN_TTL_SECONDS=3600
```

The app uses the configured version label in the UI and redirects the `Download Site Agent`
button to either the fixed URL or a freshly generated presigned S3 URL.
