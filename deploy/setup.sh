#!/bin/bash
# Jarvis Server Setup Script — Oracle Free Tier (Ubuntu/ARM64)
# Run as root: sudo bash setup.sh

set -e

echo "=== Jarvis Server Setup ==="

# 1. System packages
echo "[1/8] Installing system packages..."
apt update && apt install -y python3.11 python3.11-venv python3.11-dev \
    nginx certbot python3-certbot-nginx \
    redis-server git curl

# 2. Create jarvis user
echo "[2/8] Creating jarvis user..."
useradd -m -s /bin/bash jarvis 2>/dev/null || true

# 3. Clone/copy project
echo "[3/8] Setting up project..."
mkdir -p /opt/jarvis
cp -r server /opt/jarvis/
cp -r dashboard /opt/jarvis/
cp deploy/nginx.conf /etc/nginx/sites-available/jarvis
chown -R jarvis:jarvis /opt/jarvis

# 4. Setup Python venv for server
echo "[4/8] Setting up Python environment..."
su - jarvis -c "
cd /opt/jarvis/server
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
"

# 5. Build dashboard
echo "[5/8] Building dashboard..."
if command -v node &> /dev/null; then
    cd /opt/jarvis/dashboard
    npm install
    npm run build
else
    echo "Node.js not installed. Install with: curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt install -y nodejs"
    echo "Then run: cd /opt/jarvis/dashboard && npm install && npm run build"
fi

# 6. Setup .env
echo "[6/8] Setting up environment..."
if [ ! -f /opt/jarvis/server/.env ]; then
    cp /opt/jarvis/server/.env.example /opt/jarvis/server/.env
    echo ">>> EDIT /opt/jarvis/server/.env with your API keys <<<"
fi

# 7. Setup systemd
echo "[7/8] Setting up systemd services..."
cp deploy/jarvis-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable jarvis-server

# 8. Setup Nginx
echo "[8/8] Setting up Nginx..."
ln -sf /etc/nginx/sites-available/jarvis /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/jarvis/server/.env with your API keys"
echo "  2. Update domain in /etc/nginx/sites-available/jarvis"
echo "  3. Get SSL cert: certbot --nginx -d jarvis.yourdomain.com"
echo "  4. Start server: systemctl start jarvis-server"
echo "  5. Check status: systemctl status jarvis-server"
echo "  6. View logs: journalctl -u jarvis-server -f"
echo ""
echo "Firewall (Oracle Cloud):"
echo "  - Open ports 80, 443 in Security List"
echo "  - sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT"
echo "  - sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT"
