# Zitadel Setup Guide

This guide walks you through setting up Zitadel authentication for OntoKit development.

## Quick Start

The Docker Compose stack automatically sets up Zitadel with Login V2 and creates an admin user.

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

### 2. Access Zitadel Console

1. Open: **http://localhost:8080/ui/console**
2. Login with:
   - **Username:** `admin@OntoKit.localhost`
   - **Password:** `Admin123!`

### 3. Create OIDC Application (Automated)

The setup script can automatically create the OIDC application:

```bash
# Get the admin PAT token
docker cp ontokit-zitadel:/zitadel-data/admin.pat /tmp/admin.pat
PAT=$(cat /tmp/admin.pat)

# Create project
PROJECT_ID=$(curl -s -X POST "http://localhost:8080/management/v1/projects" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{"name": "OntoKit"}' | jq -r '.id')

# Create OIDC application
curl -s -X POST "http://localhost:8080/management/v1/projects/${PROJECT_ID}/apps/oidc" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OntoKit Web",
    "redirectUris": ["http://localhost:3000/api/auth/callback/zitadel"],
    "postLogoutRedirectUris": ["http://localhost:3000"],
    "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
    "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE", "OIDC_GRANT_TYPE_REFRESH_TOKEN"],
    "appType": "OIDC_APP_TYPE_WEB",
    "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
    "devMode": true
  }'
```

Copy the `clientId` and `clientSecret` from the response to your `.env.local` file.

---

## Manual Setup (Alternative)

## Step 1: Access Zitadel Console

1. Open your browser and navigate to: **http://localhost:8080/ui/console**
2. Login with `admin@OntoKit.localhost` / `Admin123!`
3. Login with the admin credentials:
   - **Username:** `admin`
   - **Password:** `Admin123!`
4. You will be prompted to change the password on first login - set a new password you'll remember

## Step 2: Create the OntoKit Project

1. In the Zitadel console, click **"Projects"** in the left sidebar
2. Click **"Create New Project"**
3. Set the project name to: **`OntoKit`**
4. Click **"Continue"**

## Step 3: Create the Web Application (for Next.js frontend)

1. Inside the OntoKit project, click **"New"** button
2. Select **"Web"** application type
3. Configure the application:
   - **Name:** `OntoKit Web`
   - **Authentication Method:** `CODE` (Authorization Code with PKCE)
4. Click **"Continue"**
5. Configure redirect URIs:
   - **Redirect URIs:**
     - `http://localhost:3000/api/auth/callback/zitadel`
   - **Post Logout URIs:**
     - `http://localhost:3000`
6. Click **"Create"**
7. **Important:** Copy and save the **Client ID** and **Client Secret** that are displayed

## Step 4: Create the Native Application (for Desktop clients)

1. Inside the OntoKit project, click **"New"** button
2. Select **"Native"** application type
3. Configure the application:
   - **Name:** `OntoKit Desktop`
   - **Authentication Method:** `PKCE` (no secret, public client)
4. Click **"Continue"**
5. Configure redirect URIs:
   - **Redirect URIs:**
     - `http://localhost:8400/callback`
6. Enable **Device Code** grant type in the application settings
7. Click **"Create"**
8. Copy and save the **Client ID**

## Step 5: Configure Environment Variables

### ontokit-web/.env.local

Create or update the `.env.local` file in the web project:

```bash
# Zitadel OIDC Configuration
ZITADEL_ISSUER=http://localhost:8080
ZITADEL_CLIENT_ID=<your-web-client-id>
ZITADEL_CLIENT_SECRET=<your-web-client-secret>

# NextAuth Configuration
AUTH_SECRET=<generate-a-random-secret>
AUTH_URL=http://localhost:3000
```

Generate a secret with:
```bash
openssl rand -base64 32
```

### ontokit-api/.env

Update the `.env` file in the API project:

```bash
# Zitadel Configuration
ZITADEL_ISSUER=http://localhost:8080
```

## Step 6: Restart the Applications

After updating the environment variables, restart both applications:

```bash
# Restart the API (in ontokit-api directory)
# If using uvicorn directly, stop and start it again

# Restart the web app (in ontokit-web directory)
npm run dev
```

## Testing the Authentication

1. Navigate to http://localhost:3000
2. Click the Sign In button
3. You should be redirected to Zitadel login
4. Login with your Zitadel admin account
5. After successful login, you'll be redirected back to the application

## Troubleshooting

### "Invalid redirect URI" error
Make sure the redirect URIs in Zitadel exactly match what's configured in your application.

### "Invalid client" error
Double-check the Client ID and Client Secret in your environment variables.

### Token refresh issues
Make sure you requested the `offline_access` scope (this is configured in auth.ts).

## Development Users

For development, you can create additional test users in Zitadel:

1. Go to **Users** in the left sidebar
2. Click **"New"**
3. Fill in the user details
4. The user can then login to OntoKit

## Security Notes

- The default admin password should be changed immediately
- In production, use proper TLS certificates
- Never commit secrets to version control
