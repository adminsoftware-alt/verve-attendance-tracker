# ==============================================================================
# DOCKERFILE FOR CLOUD RUN - Breakout Room Calibrator v2.0
# ==============================================================================
#
# SCALABLE DEPLOYMENT:
# - Cloud Run auto-scales based on traffic (0 to 1000 instances)
# - Min instances = 1 keeps server warm for webhooks
# - No ngrok needed - Cloud Run provides public HTTPS URL
#
# WORKFLOW:
# 1. HR joins meeting as "Scout Bot"
# 2. Opens Zoom App -> Runs calibration -> Mappings stored
# 3. Scout Bot can leave after calibration
# 4. Webhooks capture all participant activity
# 5. Daily report generated and emailed as CSV

FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY report_generator.py .

# Copy Zoom SDK app build (React frontend)
COPY breakout-calibrator/build ./breakout-calibrator/build

# Environment
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Production server with gunicorn
# --workers 2: Handle concurrent requests
# --threads 4: Thread pool per worker
# --timeout 120: Allow slow webhook processing
CMD exec gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 120 app:app
