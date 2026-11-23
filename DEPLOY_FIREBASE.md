# Hosting OAMS on Firebase (Python Flask + Cloud Run)

## Why Firebase Hosting + Cloud Run?
**Firebase Hosting** is a static CDN designed for HTML, CSS, and JavaScript. Because this application is a **full-stack Python Flask** server with an SQLite database, Jinja2 server templates, and PDF/Excel generators, Firebase routes all dynamic web traffic to **Google Cloud Run** using the `firebase.json` configuration we created.

---

## Prerequisites
1. A Google / Firebase Account: [https://console.firebase.google.com](https://console.firebase.google.com)
2. Create a new Firebase project (e.g. `oams-erp-demo`).
3. Ensure the project is on the **Blaze (Pay as you go)** plan (Google Cloud Run requires Blaze; the free tier includes 2 million free requests per month).

---

## Step-by-Step Deployment Instructions

### Step 1: Login to Firebase CLI
Run the following in your terminal to authenticate with Google:
```bash
cmd /c "npx firebase-tools login"
```
*(A browser window will open asking you to sign in with your Google account).*

### Step 2: Initialize Firebase Project
Select your Firebase project:
```bash
cd oams
cmd /c "npx firebase-tools use --add"
```
*(Choose your Firebase project ID from the list).*

### Step 3: Deploy to Cloud Run & Firebase Hosting

#### Option A: One-Command Deployment via Google Cloud SDK (`gcloud`)
```bash
# 1. Build and deploy container to Cloud Run
gcloud run deploy oams-app --source . --region asia-south1 --allow-unauthenticated

# 2. Connect Firebase Hosting to Cloud Run
npx firebase-tools deploy --only hosting
```

Your app will be live at: `https://<your-project-id>.web.app`

---

## Simpler & 100% Free Alternatives for Python Flask:
If you prefer not to configure Google Cloud billing:

1. **Render.com (Zero-config Free Hosting)**:
   - Connect your GitHub repository at [render.com](https://render.com).
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python manage.py demo && gunicorn -b 0.0.0.0:$PORT 'app:create_app()'`
   - Live in 2 minutes with a free `.onrender.com` URL!

2. **Instant Public URL (Cloudflare Tunnel / ngrok)**:
   - To immediately share or test this local app on your phone:
   ```bash
   npx cloudflared tunnel --url http://localhost:5000
   ```
   *(Provides an instant secure `https://...trycloudflare.com` public URL).*
