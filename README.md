# Attendance Middleware Server

A FastAPI-based middleware server that bridges mobile attendance apps with ZKTeco ADMS servers. Provides REST APIs for clock-in/out, GPS geofencing, biometric verification, and offline punch sync.

## 🏗️ Architecture

```
┌─────────────────┐     HTTPS      ┌──────────────────┐     iClock 501/502     ┌──────────────┐
│  Flutter Mobile  │ ────────────▶ │  Middleware API  │ ────────────────────▶ │  ADMS Server  │
│     App          │               │   (FastAPI)      │                       │   (ZKTeco)    │
└─────────────────┘               └──────────────────┘                       └──────────────┘
                                        │
                                   ┌────┴────┐
                                   │         │
                              ┌────────┐ ┌────────┐
                              │PostgreSQL│ │ Redis  │
                              │ (Data)  │ │(Cache) │
                              └────────┘ └────────┘
                                   │
                              ┌─────────┐
                              │  ARQ    │
                              │ Worker  │
                              └─────────┘
```

## ✨ Features

### Core
- **REST API** for mobile clock-in/clock-out with idempotent submission (UUID-based)
- **GPS Geofencing** — Haversine-based distance calculation with configurable branch radii
- **Multi-branch support** — Devices can be assigned to multiple office locations
- **Offline punch sync** — Batch endpoint for syncing queued offline punches
- **ADMS integration** — ZKTeco iClock 501/502 protocol bridge with heartbeat and retry
- **Admin Web UI** — Full management dashboard (Jinja2 templates)

### Security
- **API Key authentication** — bcrypt-hashed keys with expiration and revocation
- **Rate limiting** — 10 req/min on punches, 30 req/min on config (via slowapi)
- **Server-side validation** — Timestamp deviation check (±5 min), daily limit (10/day), duplicate detection (5 min window)
- **Mock location flagging** — Client-reported spoofing attempts logged for admin review
- **JWT admin auth** — Session-based authentication for the web dashboard

### Performance & Reliability
- **Redis caching** — Device config cached for 5 minutes, punch types for 10 minutes
- **ARQ task queue** — Persistent background jobs with exponential retry backoff (10s→160s)
- **Cursor-based pagination** — For large history queries
- **Streaming CSV export** — Server-side streaming for large datasets
- **Database indexes** — Composite indexes on PunchLog (employee+date, sync status)
- **Health endpoint** — `GET /health` with DB + ADMS connectivity status

### Monitoring
- **Structured logging** — JSON-format logs via structlog
- **ADMS Sync Dashboard** — Real-time sync stats, failure view, manual retry
- **Health checks** — Container orchestration ready

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- Python 3.12+ (for local development)

### Using Docker (Recommended)

```bash
# Clone the repository
git clone <repo-url>
cd backend

# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

### Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up PostgreSQL and Redis (or use Docker for these)
# Update database_url in your .env file

# Run the server
uvicorn app.main:app --reload --port 8999

# Run the ARQ worker (in a separate terminal)
python -m arq app.worker.WorkerSettings
```

## ⚙️ Configuration

### Environment Variables (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://attendance:attendance123@db:5432/attendance_db` |
| `REDIS_URL` | Redis connection string | `redis://redis:6379/0` |
| `SECRET_KEY` | JWT signing secret | `change-this-in-production` |
| `JWT_ALGORITHM` | JWT algorithm | `HS256` |
| `MIN_APP_VERSION` | Minimum mobile app version | `1.0.0` |
| `MAX_DAILY_PUNCHES` | Max punches per employee per day | `10` |
| `MAX_TIMESTAMP_DEVIATION_SECONDS` | Allowed client/server time difference | `300` |
| `ENV` | Environment (`development`/`production`) | `development` |

### Docker Compose Services

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| `attendance_middleware` | FastAPI server | `8999` | Main API + admin UI |
| `attendance_db` | PostgreSQL 15 | `5432` | Primary data store |
| `redis` | Redis 7 | `6379` | Cache + task queue broker |
| `worker` | ARQ worker | — | Background job processor |

## 📚 API Reference

### Mobile API (API Key Auth)

All mobile endpoints require `X-API-Key` header.

#### Status & Config
```
GET  /api/v1/app-status          # Version check
GET  /api/v1/device-config        # Device binding, branches, punch types
GET  /api/v1/punch-types          # Available punch types
```

#### Punch Submission
```
POST /api/v1/punch                # Single punch (idempotent via client_punch_id)
POST /api/v1/punch/batch          # Batch sync offline punches
POST /api/v1/punch/selfie         # Upload selfie image for a punch
```

#### Device Management
```
POST /api/v1/device/fcm-token     # Register FCM push notification token
```

#### Supervisor Endpoints
```
GET  /api/v1/supervisor/team                    # Team attendance status
GET  /api/v1/supervisor/team/{id}/history       # Employee punch history
POST /api/v1/attendance/correction              # Submit correction request
GET  /api/v1/supervisor/corrections             # Pending corrections
POST /api/v1/supervisor/corrections/{id}/review # Approve/reject correction
```

### Admin UI (Session Auth)

```
GET  /                     # Admin dashboard
GET  /login                # Admin login page
GET  /health               # Health check (no auth required)
GET  /ui/help              # Documentation
GET  /ui/supervisors       # Supervisor management
POST /ui/adms-sync         # Trigger manual ADMS sync
GET  /ui/logs/export       # CSV export with filters
```

## 🗄️ Database Schema

### Core Tables
- **`device_bindings`** — Registered mobile devices (employee_id, device_uuid, status, fcm_token)
- **`admin_users`** — Dashboard login accounts (username, hashed_password)
- **`branches`** — Office locations for geofencing (lat, lng, radius, qr_code_data)
- **`binding_branches`** — Many-to-many device ↔ branch assignments
- **`api_keys`** — Mobile client authentication (hashed_key, expires_at, last_used_ip)
- **`punch_types`** — Configurable attendance event types (code, label, color, geofence_required)
- **`employees`** — Employee records (employee_id, name, is_active)
- **`punch_logs`** — Attendance records (employee_id, timestamp, type, coords, flags, sync_status)

### Supervisor Tables
- **`employee_supervisors`** — Supervisor → team member mappings
- **`attendance_corrections`** — Correction requests (type, description, status, reviewed_by)

### ADMS Tables
- **`adms_targets`** — ADMS server configuration (url, serial, device_name)
- **`adms_credentials`** — ADMS auth (encrypted username/password)

## 🧪 Testing

```bash
# Run backend tests
cd backend
pytest tests/ -v

# With coverage
pytest tests/ --cov=app -v

# View coverage report
pytest tests/ --cov=app --cov-report=html
open htmlcov/index.html
```

## 🐳 Deployment

### Docker Deployment
```bash
# Build and start
docker-compose up -d --build

# Scale workers (for high volume)
docker-compose up -d --scale worker=3

# Monitoring
docker-compose logs -f worker  # Watch sync activity
```

### Security Checklist for Production
1. Change `SECRET_KEY` in environment
2. Enable HTTPS with valid TLS certificate
3. Set strong database passwords
4. Enable certificate pinning on mobile app
5. Configure rate limiting thresholds
6. Set API key expiration policies
7. Enable structured JSON logging for log aggregation
8. Set up health check monitoring
9. Configure regular database backups
10. Restrict CORS origins to your mobile app domain

## 📊 ADMS Integration

The middleware communicates with ZKTeco ADMS servers using the iClock 501/502 protocol:

- **Handshake**: `GET /iclock/getrequest?SN={serial}&options=...`
- **Push punches**: `GET /iclock/device/...?table=ATT_LOG&...`
- **Ack**: `POST /iclock/device/.../ack`
- **Retry**: Exponential backoff via ARQ worker (10s → 160s, max 5 attempts)
- **Scheduled retry**: Failed records retried every 5 minutes

## 🔐 Security Model

```
┌─────────────────────────────────────────────┐
│              Security Layers                 │
├─────────────────────────────────────────────┤
│ 1. API Key (bcrypt-hashed + expiration)     │
│ 2. Rate Limiting (10/min punch)             │
│ 3. Timestamp Validation (±5 min)            │
│ 4. Duplicate Detection (5 min window)       │
│ 5. Daily Punch Limit (10/day)               │
│ 6. Mock Location Detection                  │
│ 7. Root/Jailbreak Detection (mobile)        │
│ 8. Certificate Pinning (mobile)             │
│ 9. Biometric Auth (mobile)                  │
└─────────────────────────────────────────────┘
```

## 📝 License

[Your License Here]
