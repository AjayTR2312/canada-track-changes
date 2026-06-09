#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# EC2 Deployment Script for PXML Document Processor
# Run this on a fresh Ubuntu 22.04+ EC2 instance
# Recommended: t3.2xlarge (8 vCPU, 32GB RAM) or larger
# ═══════════════════════════════════════════════════════════════

set -e

echo "═══ PXML Pipeline - EC2 Deployment Setup ═══"

# 1. Update system
echo "[1/6] Updating system packages..."
sudo apt-get update && sudo apt-get upgrade -y

# 2. Install Docker
echo "[2/6] Installing Docker..."
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add current user to docker group
sudo usermod -aG docker $USER

# 3. Install AWS CLI
echo "[3/6] Installing AWS CLI..."
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
sudo apt-get install -y unzip
unzip -q awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip

# 4. Configure AWS (skip if using IAM instance role)
echo "[4/6] Configuring AWS..."
echo "If using IAM instance role, press Ctrl+C and skip this step."
echo "Otherwise, run: aws configure"

# 5. Create S3 bucket
echo "[5/6] Creating S3 bucket..."
S3_BUCKET=${S3_BUCKET_NAME:-pxml-pipeline-bucket}
AWS_REGION=${AWS_REGION:-us-east-1}
aws s3 mb s3://$S3_BUCKET --region $AWS_REGION 2>/dev/null || echo "Bucket already exists or cannot be created"

# 6. Deploy application
echo "[6/6] Building and starting application..."
cd /home/ubuntu/pxml-pipeline  # Adjust path as needed

# Copy .env.example to .env and edit
if [ ! -f .env ]; then
    cp .env.example .env
    echo "⚠️  Please edit .env file with your AWS credentials and S3 bucket name"
    echo "   nano .env"
fi

# Build and start
docker compose build
docker compose up -d

echo ""
echo "═══════════════════════════════════════════════════"
echo "✅ Deployment complete!"
echo ""
echo "  Streamlit UI: http://<EC2-PUBLIC-IP>:8501"
echo "  FastAPI Docs: http://<EC2-PUBLIC-IP>:8000/docs"
echo "  Health Check: http://<EC2-PUBLIC-IP>:8000/api/health"
echo ""
echo "  Logs: docker compose logs -f"
echo "  Stop: docker compose down"
echo "═══════════════════════════════════════════════════"
