# US Forms PXML Pipeline - React Frontend

React-based frontend for the XML Track Changes Pipeline, replacing the Streamlit UI.

## Quick Start

```bash
cd frontend
npm install
npm run dev
```

Opens at http://localhost:4000. The Vite dev server proxies `/api/*` requests to `http://localhost:8000` (the FastAPI backend).

## Structure

```
src/
├── main.tsx                  # Entry point
├── App.tsx                   # Root component (auth gate)
├── index.css                 # Global styles & reset
├── context/
│   ├── AuthContext.tsx        # Login/register state
│   └── JobContext.tsx         # Job status polling
├── services/
│   └── api.ts                # Axios API client (all backend calls)
├── pages/
│   ├── LoginPage.tsx          # Sign-in & registration
│   ├── LoginPage.css
│   ├── Dashboard.tsx          # Main layout with sidebar + tabs
│   └── Dashboard.css
└── components/
    ├── ProcessingTab.tsx      # File upload, start, live metrics, pipeline steps
    ├── ProcessingTab.css
    ├── DownloadsTab.tsx       # Download output ZIP & validation report
    ├── ValidationTab.tsx      # Run/re-run validation, list files
    ├── SystemLogsTab.tsx      # Full processing log viewer
    ├── AccountTab.tsx         # Profile editor + usage logs table
    └── Tabs.css               # Shared tab component styles
```

## Screens (matching Streamlit)

| Streamlit Tab | React Component |
|---|---|
| Login Gate | `LoginPage` |
| Sidebar (upload, config) | `Dashboard` sidebar |
| 🎯 Processing | `ProcessingTab` |
| 📥 Downloads | `DownloadsTab` |
| 🔍 Validation | `ValidationTab` |
| 📜 System Logs | `SystemLogsTab` |
| 👤 Account & Usage | `AccountTab` |

## Backend API Endpoints Used

- `POST /api/process` — Upload files & start pipeline
- `GET /api/status/{job_id}` — Poll job status
- `GET /api/log/{job_id}` — Get processing log
- `GET /api/download/{job_id}` — Download output ZIP
- `GET /api/download-validation/{job_id}` — Download validation Excel
- `POST /api/validate/{job_id}` — Run validation
- `GET /api/list-outputs/{job_id}` — List output files
- `POST /api/auth/login` — Login by employee ID
- `POST /api/auth/register` — Register new user
- `PUT /api/auth/update` — Update user profile
- `GET /api/auth/usage-logs` — Get usage logs

## Production Build

```bash
npm run build
```

Output goes to `dist/`. Serve it with any static file server or configure nginx to serve the dist folder and proxy `/api` to the FastAPI backend.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `VITE_API_URL` | Backend API base URL | `""` (uses proxy in dev) |
