# Zitadel Setup Guide

This guide walks you through setting up Zitadel authentication for OntoKit development.

## Quick Start

The Docker Compose stack automatically starts Zitadel with Login V2 and creates an admin user. After starting the stack, run the setup script to create the OIDC applications.

### 1. Start the Docker Stack

```bash
cd ontokit-api
docker compose up -d
```

Wait for all services to be healthy:
```bash
docker compose ps
# All services should show "healthy"
```

### 2. Run the Setup Script

The `setup-zitadel.sh` script automates OIDC application creation and credential configuration:

```bash
./scripts/setup-zitadel.sh --update-env
```

This will:
1. Wait for Zitadel to be ready
2. Retrieve the admin PAT (Personal Access Token) from the Zitadel data volume
3. Create an "OntoKit" project (or find the existing one)
4. Create an "OntoKit Web" OIDC application (or reuse existing)
5. Update both `ontokit-api/.env` and `ontokit-web/.env.local` with the credentials
6. Prompt to recreate the API and worker containers to pick up the new credentials

### 3. Recreate API Containers

If the script didn't already prompt you, recreate the API and worker containers so they pick up the new environment variables:

```bash
docker compose up -d --force-recreate api worker
```

### 4. Access Zitadel Console

1. Open: **http://localhost:8080/ui/console**
2. Login with:
   - **Username:** `admin@ontokit.localhost`
   - **Password:** `Admin123!`

---

## Script Options

The setup script supports several flags:

| Flag | Description |
|------|-------------|
| *(no flags)* | Display credentials only (does not update files) |
| `--update-env` | Update `.env` files with credentials and prompt to recreate containers |
| `--docker-init` | Start Docker stack if not running, then configure |
| `--force-secrets` | Regenerate client secrets (invalidates existing sessions) |

Flags can be combined:

```bash
# Full automated setup from scratch
./scripts/setup-zitadel.sh --update-env --docker-init

# Regenerate secrets and update env files
./scripts/setup-zitadel.sh --update-env --force-secrets
```

### Idempotent Behavior

The script is safe to run multiple times:
- If the OntoKit project already exists, it reuses it
- If the OIDC app already exists, it retrieves the existing client ID
- If a matching client secret is found in the `.env` files, it reuses it (preserving sessions)
- Use `--force-secrets` to explicitly regenerate secrets

---

## Environment Files Updated

When using `--update-env`, the script updates these files:

**ontokit-api/.env:**
```bash
ZITADEL_CLIENT_ID=<client-id>
ZITADEL_CLIENT_SECRET=<client-secret>
ZITADEL_SERVICE_TOKEN=<admin-pat>
SUPERADMIN_USER_IDS=<admin-user-id>
```

**ontokit-web/.env.local:**
```bash
ZITADEL_CLIENT_ID=<client-id>
NEXT_PUBLIC_ZITADEL_CLIENT_ID=<client-id>
ZITADEL_CLIENT_SECRET=<client-secret>
```

---

## Manual Setup (Alternative)

If the automated script doesn't work for your environment, you can configure Zitadel manually.

### Step 1: Access Zitadel Console

1. Open your browser and navigate to: **http://localhost:8080/ui/console**
2. Login with:
   - **Username:** `admin@ontokit.localhost`
   - **Password:** `Admin123!`

### Step 2: Create the OntoKit Project

1. In the Zitadel console, click **"Projects"** in the left sidebar
2. Click **"Create New Project"**
3. Set the project name to: **`OntoKit`**
4. Click **"Continue"**

### Step 3: Create the Web Application (for Next.js frontend)

1. Inside the OntoKit project, click **"New"** button
2. Select **"Web"** application type
3. Configure the application:
   - **Name:** `OntoKit Web`
   - **Authentication Method:** `CODE` (Authorization Code)
4. Click **"Continue"**
5. Configure redirect URIs:
   - **Redirect URIs:** `http://localhost:3000/api/auth/callback/zitadel`
   - **Post Logout URIs:** `http://localhost:3000`
6. Click **"Create"**
7. **Important:** Copy and save the **Client ID** and **Client Secret** that are displayed

### Step 4: Configure Environment Variables

**ontokit-api/.env:**
```bash
ZITADEL_ISSUER=http://localhost:8080
ZITADEL_CLIENT_ID=<your-client-id>
ZITADEL_CLIENT_SECRET=<your-client-secret>
```

**ontokit-web/.env.local:**
```bash
ZITADEL_ISSUER=http://localhost:8080
ZITADEL_CLIENT_ID=<your-client-id>
NEXT_PUBLIC_ZITADEL_CLIENT_ID=<your-client-id>
ZITADEL_CLIENT_SECRET=<your-client-secret>
AUTH_SECRET=<generate-a-random-secret>
AUTH_URL=http://localhost:3000
```

Generate a secret with:
```bash
openssl rand -base64 32
```

### Step 5: Restart Services

After updating environment files, restart the services to pick up the new credentials:

```bash
# Full Docker mode
docker compose up -d --force-recreate api worker

# Hybrid mode — restart your local uvicorn process and the Next.js dev server
```

---

## After Resetting the Database

If you ran `docker compose down -v` (which destroys volumes), Zitadel will reinitialize from scratch. You need to re-run the setup:

```bash
docker compose up -d
# Wait for Zitadel to be ready
./scripts/setup-zitadel.sh --update-env
docker compose up -d --force-recreate api worker
```

---

## Troubleshooting

### "Invalid redirect URI" error
Make sure the redirect URIs in Zitadel exactly match what's configured in your application.

### "Invalid client" error
Double-check the Client ID and Client Secret in your environment variables. You may need to regenerate secrets:
```bash
./scripts/setup-zitadel.sh --update-env --force-secrets
```

### Token refresh issues
Make sure you requested the `offline_access` scope (this is configured in `auth.ts`).

### Script can't find admin PAT
Ensure the Zitadel container is running and healthy:
```bash
docker compose ps
docker compose logs zitadel
```

## Development Users

For development, you can create additional test users in Zitadel:

1. Go to **Users** in the left sidebar
2. Click **"New"**
3. Fill in the user details
4. The user can then login to OntoKit

## Security Notes

- The default admin password should be changed immediately in production
- In production, use proper TLS certificates
- Never commit secrets to version control
