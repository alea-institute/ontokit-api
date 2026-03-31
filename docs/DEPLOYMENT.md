# Deployment Guide: OntoKit FOLIO

This documents deploying the OntoKit FOLIO adapter (API + Web) on an Ubuntu server with Caddy for HTTPS.

## Architecture

```
Internet
  |
  v
Caddy (port 443, auto-SSL)
  |
  +-- /api/v1/*  -->  ontokit-api (uvicorn, port 8000)
  +-- /health    -->  ontokit-api
  +-- /*         -->  ontokit-web (next start, port 3000)
```

- **ontokit-api**: Python FastAPI app using `folio-python` to serve the FOLIO ontology (18,000+ classes)
- **ontokit-web**: Next.js 15 frontend providing the tree browser, class detail viewer, search, and editor UI
- **Caddy**: Reverse proxy with automatic Let's Encrypt SSL

## Prerequisites

- Ubuntu 24.04 (tested on ARM64/aarch64, works on x86_64 too)
- 2+ CPU cores, 4+ GB RAM (FOLIO loads ~300MB into memory)
- DNS A record pointing your domain to the server IP
- Ports 80 and 443 open (for Caddy / Let's Encrypt)

## Server Setup

### 1. Install system dependencies

```bash
# Caddy
sudo apt-get update
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy git

# Node.js 22
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone repositories

```bash
cd /home/ubuntu
git clone -b feature/folio-adapter https://github.com/alea-institute/ontokit-api.git
git clone -b feature/folio-adapter https://github.com/alea-institute/ontokit-web.git
```

### 3. Build the API

```bash
cd /home/ubuntu/ontokit-api
uv venv --python 3.12
uv pip install -e "." "folio-python[search]"
```

Test it works:

```bash
.venv/bin/uvicorn ontokit.main:app --host 127.0.0.1 --port 8000
# Wait ~15 seconds for FOLIO to load, then:
# curl http://127.0.0.1:8000/health
# Should return: {"status":"healthy","ontology":"FOLIO","classes":18326,"properties":175}
```

### 4. Build the web frontend

```bash
cd /home/ubuntu/ontokit-web

# Create environment file (replace YOUR_DOMAIN)
cat > .env.local << 'EOF'
NEXT_PUBLIC_API_URL=https://YOUR_DOMAIN
NEXTAUTH_URL=https://YOUR_DOMAIN
NEXTAUTH_SECRET=$(openssl rand -hex 32)
AUTH_SECRET=$(openssl rand -hex 32)
EOF

npm install
npm run build
```

### 5. Create systemd services

**API service** (`/etc/systemd/system/ontokit-api.service`):

```ini
[Unit]
Description=OntoKit FOLIO API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ontokit-api
ExecStart=/home/ubuntu/ontokit-api/.venv/bin/uvicorn ontokit.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
Environment=PATH=/home/ubuntu/.local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

**Web service** (`/etc/systemd/system/ontokit-web.service`):

```ini
[Unit]
Description=OntoKit FOLIO Web
After=network.target ontokit-api.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ontokit-web
ExecStart=/usr/bin/node node_modules/.bin/next start -p 3000
Restart=always
RestartSec=5
EnvironmentFile=/home/ubuntu/ontokit-web/.env.local

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ontokit-api ontokit-web

# Start API first (needs ~15s to load FOLIO into memory)
sudo systemctl start ontokit-api
sleep 20
sudo systemctl start ontokit-web
```

### 6. Configure Caddy

Edit `/etc/caddy/Caddyfile` (replace `YOUR_DOMAIN`):

```
YOUR_DOMAIN {
    # API routes -> Python backend
    handle /api/v1/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /health {
        reverse_proxy 127.0.0.1:8000
    }
    handle /docs {
        reverse_proxy 127.0.0.1:8000
    }
    handle /openapi.json {
        reverse_proxy 127.0.0.1:8000
    }

    # Everything else -> Next.js frontend
    handle {
        reverse_proxy 127.0.0.1:3000
    }
}
```

```bash
sudo systemctl restart caddy
```

Caddy will automatically obtain and renew SSL certificates from Let's Encrypt.

## Verification

```bash
# Check all services are running
sudo systemctl is-active ontokit-api ontokit-web caddy

# Check API health
curl https://YOUR_DOMAIN/health

# Check frontend
curl -s -o /dev/null -w "%{http_code}" https://YOUR_DOMAIN/projects
```

## Operations

### View logs

```bash
sudo journalctl -u ontokit-api -f    # API logs
sudo journalctl -u ontokit-web -f    # Web logs
sudo journalctl -u caddy -f          # Caddy logs
```

### Restart services

```bash
sudo systemctl restart ontokit-api   # ~15s to reload FOLIO
sudo systemctl restart ontokit-web
sudo systemctl restart caddy
```

### Update code

```bash
# API
cd /home/ubuntu/ontokit-api
git pull
uv pip install -e "."
sudo systemctl restart ontokit-api

# Web
cd /home/ubuntu/ontokit-web
git pull
npm install
npm run build
sudo systemctl restart ontokit-web
```

## Current Deployment

- **Domain**: ontokit.openlegalstandard.org
- **Server**: 54.224.195.12 (AWS, Ubuntu 24.04 ARM64)
- **SSH**: `ssh -i ~/.ssh/alea/folio-ontokit.pem ubuntu@54.224.195.12`
- **Branch**: `feature/folio-adapter` on both repos
