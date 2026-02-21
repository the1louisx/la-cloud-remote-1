# LA Software Cloud Remote - Deployment Guide

## 1. Backend Deployment (Render.com)

### Step 1: Create a GitHub Repository

1. Go to https://github.com/new
2. Create a new repository (e.g., `la-cloud-remote`)
3. Clone it locally or use GitHub's web interface
4. Add these files to the repo:
   - `main.py`
   - `requirements.txt`
5. Commit and push:
   ```bash
   git add main.py requirements.txt
   git commit -m "Initial backend"
   git push origin main
   ```

### Step 2: Deploy on Render

1. Go to https://render.com and sign in (or create account)
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub account if not already connected
4. Select your `la-cloud-remote` repository
5. Configure the service:

   | Setting | Value |
   |---------|-------|
   | **Name** | `la-server` (or any name you like) |
   | **Region** | Choose closest to you |
   | **Branch** | `main` |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `uvicorn main:app --host 0.0.0.0 --port 10000` |

6. Select the **Free** plan
7. Click **"Create Web Service"**

### Step 3: Wait for Deployment

- Render will build and deploy your app (takes 2-3 minutes)
- Once complete, you'll get a URL like:
  ```
  https://la-server-xxxx.onrender.com
  ```
- Test it by visiting the URL - you should see:
  ```json
  {"status": "ok", "message": "LA server running"}
  ```

### Your Backend URL

Your final backend URL will be something like:
```
https://la-server-xxxx.onrender.com
```

**Save this URL** - you'll need it for both the Mac app and the phone web page.

---

## 2. Phone Web Page Deployment

### Option A: GitHub Pages (Free & Easy)

1. Create a new GitHub repo (e.g., `la-remote`)
2. Add `remote.html` as `index.html`
3. **IMPORTANT**: Edit line 97 to set your Render backend URL:
   ```javascript
   const BACKEND_URL = 'https://la-server-xxxx.onrender.com';
   ```
4. Go to repo **Settings** → **Pages**
5. Set Source to "Deploy from a branch" → `main` → `/ (root)`
6. Your page will be at: `https://yourusername.github.io/la-remote/`

### Option B: Netlify (Also Free)

1. Go to https://netlify.com
2. Drag and drop the `remote.html` file (rename to `index.html` first)
3. You'll get a URL like `https://random-name.netlify.app`

### Option C: Your Own Domain

If you have your own domain (e.g., `la-remote.my-domain.com`):
1. Host `remote.html` (as `index.html`) on any static hosting
2. Point your domain to that hosting

---

## 3. Testing the Endpoints

### Test Health Check
```bash
curl https://la-server-xxxx.onrender.com/
```
Response:
```json
{"status": "ok", "message": "LA server running"}
```

### Test Register
```bash
curl -X POST https://la-server-xxxx.onrender.com/register \
  -H "Content-Type: application/json" \
  -d '{"pin_hash": "5994471abb01112afcc18159f6cc74b4f511b99806da59b3caf5a9c173cacfc5"}'
```
Response:
```json
{"device_id": "abc123xyz789...", "device_token": "secure_token_here..."}
```

### Test Command
```bash
curl -X POST https://la-server-xxxx.onrender.com/command \
  -H "Content-Type: application/json" \
  -d '{"device_id": "abc123xyz789...", "pin_hash": "5994471abb01112afcc18159f6cc74b4f511b99806da59b3caf5a9c173cacfc5", "command": "ARM"}'
```
Response:
```json
{"status": "ok"}
```

### Test Poll
```bash
curl -X POST https://la-server-xxxx.onrender.com/poll \
  -H "Content-Type: application/json" \
  -d '{"device_id": "abc123xyz789...", "device_token": "secure_token_here..."}'
```
Response (if command pending):
```json
{"command": "ARM"}
```
Response (if no command):
```json
{"command": null}
```

---

## Important Notes

### Free Tier Limitations

**Render Free Tier:**
- Server spins down after 15 minutes of inactivity
- First request after spindown takes ~30 seconds
- For always-on: upgrade to paid plan ($7/month)

### Security Considerations

1. **PIN is never sent in plain text** - only SHA-256 hash
2. **Device token** protects the poll endpoint from unauthorized access
3. **CORS is enabled** for the web page to work
4. **In production**, consider:
   - Adding rate limiting
   - Using a database instead of in-memory storage
   - Adding HTTPS certificate (Render provides this automatically)

### If Server Restarts

The in-memory storage will be cleared. The Mac app will need to re-register.
For persistence, you could:
- Use Render's Redis add-on
- Use a SQLite file with persistent disk
- Use an external database
