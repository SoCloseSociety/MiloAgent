#!/usr/bin/env bash
# =============================================================================
# MiloAgent — Server Deployment Script
# =============================================================================
# Usage: ./deploy.sh [command]
#   --setup         First-time setup (install deps, create dirs, .env, Nginx, SSL)
#   --up            Build & start (or restart) everything
#   --down          Stop all services
#   --restart       Restart all services
#   --update        Pull latest code, rebuild, restart
#   --logs          Tail live logs
#   --status        Show service status
#   --backup        Backup data directory
#   --ssl           Request/renew SSL certificate only
# =============================================================================

set -euo pipefail

# ── Colors ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Config ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DOMAIN="milo.yourdomain.com"          # CHANGE THIS to your domain
DEFAULT_PORT=8420
COMPOSE_FILE="docker-compose.yml"
BACKUP_DIR="$SCRIPT_DIR/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# ── Logging ─────────────────────────────────────────────
log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_header()  { echo -e "\n${BOLD}${CYAN}══════ $1 ══════${NC}\n"; }

# ── Port helpers ────────────────────────────────────────
is_port_in_use() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -tlnp 2>/dev/null | grep -q ":${port} " && return 0
    fi
    if command -v lsof &>/dev/null; then
        lsof -iTCP:"$port" -sTCP:LISTEN -t &>/dev/null && return 0
    fi
    (echo >/dev/tcp/127.0.0.1/"$port") 2>/dev/null && return 0
    return 1
}

find_available_port() {
    local port=$1
    local max=20
    local i=0
    while [ $i -lt $max ]; do
        if ! is_port_in_use "$port"; then
            echo "$port"
            return 0
        fi
        log_warn "Port $port in use, trying $((port + 1))..."
        port=$((port + 1))
        i=$((i + 1))
    done
    log_error "No available port found (tried $1-$((port - 1)))"
    return 1
}

resolve_port() {
    log_info "Checking port $DEFAULT_PORT..."
    # If our own container is using it, that's fine (we'll restart)
    local container_port
    container_port=$(docker port miloagent 8420/tcp 2>/dev/null | grep -oP ':\K[0-9]+' || true)
    if [ "$container_port" = "$DEFAULT_PORT" ]; then
        MILO_PORT=$DEFAULT_PORT
        log_success "Port $MILO_PORT (already ours)"
        return
    fi
    MILO_PORT=$(find_available_port "$DEFAULT_PORT")
    log_success "Port resolved: ${BOLD}$MILO_PORT${NC}"
}

# ── Prerequisites ───────────────────────────────────────
check_prerequisites() {
    log_header "Checking prerequisites"
    local ok=true

    if command -v docker &>/dev/null; then
        log_success "Docker $(docker --version | grep -oP '[0-9]+\.[0-9]+\.[0-9]+')"
    else
        log_error "Docker not installed"
        ok=false
    fi

    if docker compose version &>/dev/null; then
        log_success "Docker Compose $(docker compose version --short 2>/dev/null)"
    else
        log_error "Docker Compose not available"
        ok=false
    fi

    if command -v nginx &>/dev/null; then
        log_success "Nginx $(nginx -v 2>&1 | grep -oP '[0-9]+\.[0-9]+\.[0-9]+')"
    else
        log_warn "Nginx not found — will try to install during setup"
    fi

    if command -v certbot &>/dev/null; then
        log_success "Certbot $(certbot --version 2>&1 | grep -oP '[0-9]+\.[0-9]+\.[0-9]+')"
    else
        log_warn "Certbot not found — will try to install during setup"
    fi

    if [ "$ok" = false ]; then
        log_error "Missing prerequisites. Install them first."
        exit 1
    fi
}

# ── Setup (.env) ────────────────────────────────────────
setup_env() {
    if [ -f .env ]; then
        log_info ".env already exists"
        source .env
        return
    fi

    log_header "Creating .env"

    local pass
    pass=$(openssl rand -hex 12 2>/dev/null || head -c 24 /dev/urandom | xxd -p | head -c 24)

    cat > .env <<EOF
# MiloAgent — Environment Variables
# Generated on $(date)

# Web dashboard auth (REQUIRED)
MILO_WEB_USER=admin
MILO_WEB_PASS=$pass

# Port (auto-resolved if taken)
MILO_PORT=$DEFAULT_PORT

# Docker resource limits
MILO_MEM_LIMIT=512m
MILO_CPU_LIMIT=1.0

# Timezone
TZ=UTC
EOF

    log_success ".env created with password: ${BOLD}$pass${NC}"
    log_warn "Edit .env to change credentials: ${BOLD}nano .env${NC}"
}

# ── Setup Nginx ─────────────────────────────────────────
setup_nginx() {
    log_header "Configuring Nginx for $DOMAIN"

    # Install Nginx if missing
    if ! command -v nginx &>/dev/null; then
        log_info "Installing Nginx..."
        sudo apt-get update -qq && sudo apt-get install -y nginx
        log_success "Nginx installed"
    fi

    # Install Certbot if missing
    if ! command -v certbot &>/dev/null; then
        log_info "Installing Certbot..."
        sudo apt-get install -y certbot python3-certbot-nginx
        log_success "Certbot installed"
    fi

    local nginx_conf="/etc/nginx/sites-available/${DOMAIN}"
    local nginx_enabled="/etc/nginx/sites-enabled/${DOMAIN}"

    # Create Nginx config
    log_info "Writing Nginx config for $DOMAIN → localhost:${MILO_PORT}"
    sudo tee "$nginx_conf" >/dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # Certbot will add SSL redirect here
    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    # SSL certs — will be filled by certbot
    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Security headers
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    # WebSocket support
    location /ws/ {
        proxy_pass http://127.0.0.1:${MILO_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
    }

    # API & static
    location / {
        proxy_pass http://127.0.0.1:${MILO_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30;
        proxy_connect_timeout 10;
    }

    # Logs
    access_log /var/log/nginx/milo-access.log;
    error_log /var/log/nginx/milo-error.log;
}
EOF

    # Enable site
    sudo ln -sf "$nginx_conf" "$nginx_enabled"

    # Test config
    if sudo nginx -t 2>/dev/null; then
        log_success "Nginx config valid"
    else
        # SSL certs don't exist yet — write HTTP-only config first for certbot
        log_warn "SSL certs not found yet — writing HTTP-only config for certbot"
        sudo tee "$nginx_conf" >/dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:${MILO_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
        if sudo nginx -t 2>/dev/null; then
            log_success "HTTP-only Nginx config valid"
        else
            log_error "Nginx config invalid! Fix manually: sudo nginx -t"
            return 1
        fi
    fi

    sudo systemctl reload nginx
    log_success "Nginx reloaded"
}

# ── SSL Certificate ─────────────────────────────────────
setup_ssl() {
    log_header "SSL Certificate for $DOMAIN"

    if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
        log_success "SSL certificate already exists"
        # Check expiry
        local expiry
        expiry=$(openssl x509 -enddate -noout -in "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" 2>/dev/null | cut -d= -f2)
        log_info "Expires: $expiry"
        return
    fi

    log_info "Requesting SSL certificate via Certbot..."

    # Make sure Nginx is running with HTTP-only config
    sudo systemctl reload nginx 2>/dev/null || sudo systemctl start nginx

    if sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email "admin@yourdomain.com" --redirect; then
        log_success "SSL certificate obtained!"
        # Now write the full SSL config
        setup_nginx
        sudo systemctl reload nginx
    else
        log_error "Certbot failed. Check:"
        echo "  1. DNS: dig $DOMAIN +short  (should be your server IP)"
        echo "  2. Port 80 open: sudo ufw allow 80"
        echo "  3. Port 443 open: sudo ufw allow 443"
        echo "  4. Try manually: sudo certbot --nginx -d $DOMAIN"
        return 1
    fi
}

# ── Create directories ──────────────────────────────────
create_dirs() {
    local dirs=(
        "data"
        "data/cookies"
        "data/sessions"
        "logs"
        "config"
        "projects"
        "prompts"
        "backups"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
    done
    log_success "Directories created"
}

# ── Commands ────────────────────────────────────────────

cmd_setup() {
    log_header "MiloAgent — First-time Setup"
    check_prerequisites
    create_dirs
    setup_env
    source .env 2>/dev/null || true
    resolve_port

    # Update .env with resolved port
    sed -i "s/^MILO_PORT=.*/MILO_PORT=$MILO_PORT/" .env 2>/dev/null || true

    setup_nginx
    setup_ssl

    log_header "Setup complete!"
    echo -e "  Domain:    ${BOLD}https://$DOMAIN${NC}"
    echo -e "  Port:      ${BOLD}$MILO_PORT${NC}"
    echo -e "  Login:     admin / check .env"
    echo ""
    echo -e "  ${YELLOW}Next steps:${NC}"
    echo -e "  1. Edit .env if needed:               ${BOLD}nano .env${NC}"
    echo -e "  2. Configure your project:  ${BOLD}nano config/llm.yaml${NC}"
    echo -e "  3. Deploy:                             ${BOLD}./deploy.sh --up${NC}"
    echo ""
}

cmd_up() {
    log_header "MiloAgent — Build & Start"

    # Load .env
    if [ -f .env ]; then
        set -a; source .env; set +a
    else
        log_error "No .env found. Run ./deploy.sh --setup first."
        exit 1
    fi

    # Resolve port
    resolve_port
    export MILO_PORT

    # Update .env with resolved port
    sed -i "s/^MILO_PORT=.*/MILO_PORT=$MILO_PORT/" .env 2>/dev/null || true

    # Build & start
    log_info "Building Docker image..."
    docker compose -f "$COMPOSE_FILE" build --pull

    log_info "Starting services..."
    docker compose -f "$COMPOSE_FILE" up -d

    # Update Nginx if port changed
    local nginx_conf="/etc/nginx/sites-available/${DOMAIN}"
    if [ -f "$nginx_conf" ]; then
        if ! grep -q "proxy_pass http://127.0.0.1:${MILO_PORT}" "$nginx_conf"; then
            log_info "Updating Nginx proxy to port $MILO_PORT..."
            sudo sed -i "s|proxy_pass http://127.0.0.1:[0-9]*|proxy_pass http://127.0.0.1:${MILO_PORT}|g" "$nginx_conf"
            sudo nginx -t 2>/dev/null && sudo systemctl reload nginx
            log_success "Nginx updated"
        fi
    fi

    # Wait for health
    log_info "Waiting for service to be healthy..."
    local attempts=0
    while [ $attempts -lt 30 ]; do
        if curl -sf "http://localhost:$MILO_PORT/" >/dev/null 2>&1; then
            break
        fi
        sleep 2
        attempts=$((attempts + 1))
    done

    if [ $attempts -lt 30 ]; then
        log_success "MiloAgent is running!"
        echo ""
        echo -e "  Dashboard:  ${BOLD}https://$DOMAIN${NC}"
        echo -e "  Local:      ${BOLD}http://localhost:$MILO_PORT${NC}"
        echo -e "  Logs:       ${BOLD}./deploy.sh --logs${NC}"
        echo ""
    else
        log_warn "Service started but not yet responding. Check logs:"
        echo -e "  ${BOLD}./deploy.sh --logs${NC}"
    fi
}

cmd_down() {
    log_header "MiloAgent — Stopping"
    docker compose -f "$COMPOSE_FILE" down
    log_success "All services stopped"
}

cmd_restart() {
    log_header "MiloAgent — Restarting"
    if [ -f .env ]; then
        set -a; source .env; set +a
    fi
    resolve_port
    export MILO_PORT
    docker compose -f "$COMPOSE_FILE" restart
    log_success "Services restarted"
    echo -e "  Dashboard: ${BOLD}https://$DOMAIN${NC}"
}

cmd_update() {
    log_header "MiloAgent — Update"

    log_info "Pulling latest code..."
    git pull --ff-only 2>/dev/null || {
        log_warn "Git pull failed (not a git repo or conflicts). Continuing with local files."
    }

    cmd_up
}

cmd_logs() {
    docker compose -f "$COMPOSE_FILE" logs -f --tail=100
}

cmd_status() {
    log_header "MiloAgent — Status"

    # Container status
    docker compose -f "$COMPOSE_FILE" ps

    echo ""

    # Load port
    if [ -f .env ]; then
        set -a; source .env; set +a
    fi
    local port="${MILO_PORT:-$DEFAULT_PORT}"

    # Health check
    if curl -sf "http://localhost:$port/" >/dev/null 2>&1; then
        log_success "Dashboard responding on port $port"
        echo -e "  URL: ${BOLD}https://$DOMAIN${NC}"

        # Quick health check via unauthenticated endpoint
        local health
        health=$(curl -sf "http://localhost:$port/health" 2>/dev/null || echo '{"error":"no response"}')
        echo -e "  Health: $health"
    else
        log_warn "Dashboard not responding on port $port"
    fi

    # SSL check
    echo ""
    if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
        local expiry
        expiry=$(openssl x509 -enddate -noout -in "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" 2>/dev/null | cut -d= -f2)
        log_success "SSL: valid (expires $expiry)"
    else
        log_warn "SSL: no certificate found"
    fi

    # Nginx check
    if systemctl is-active --quiet nginx; then
        log_success "Nginx: running"
    else
        log_warn "Nginx: not running"
    fi

    # Disk usage
    echo ""
    local db_size
    db_size=$(du -sh data/miloagent.db 2>/dev/null | cut -f1 || echo "N/A")
    local logs_size
    logs_size=$(du -sh logs/ 2>/dev/null | cut -f1 || echo "N/A")
    echo -e "  DB size:   $db_size"
    echo -e "  Logs size: $logs_size"
}

cmd_backup() {
    log_header "MiloAgent — Backup"
    mkdir -p "$BACKUP_DIR"

    local backup_name="milo_backup_${TIMESTAMP}"
    local backup_path="$BACKUP_DIR/$backup_name"
    mkdir -p "$backup_path"

    [ -d "data" ] && cp -r data "$backup_path/" && log_success "Data backed up"
    [ -d "config" ] && cp -r config "$backup_path/" && log_success "Config backed up"
    [ -d "projects" ] && cp -r projects "$backup_path/" && log_success "Projects backed up"
    if [ -f ".env" ]; then
        cp .env "$backup_path/"
        log_success ".env backed up"
    fi

    tar -czf "$backup_path.tar.gz" -C "$BACKUP_DIR" "$backup_name"
    rm -rf "$backup_path"

    local size
    size=$(du -sh "$backup_path.tar.gz" | cut -f1)
    log_success "Backup saved: ${BOLD}$backup_path.tar.gz${NC} ($size)"

    # Cleanup old backups (keep last 5)
    ls -t "$BACKUP_DIR"/milo_backup_*.tar.gz 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true
}

cmd_ssl() {
    if [ -f .env ]; then
        set -a; source .env; set +a
    fi
    MILO_PORT="${MILO_PORT:-$DEFAULT_PORT}"
    setup_ssl
}

# ── Main ────────────────────────────────────────────────
show_usage() {
    echo -e "${BOLD}MiloAgent Deploy${NC}"
    echo ""
    echo "Usage: ./deploy.sh [command]"
    echo ""
    echo "Commands:"
    echo "  --setup      First-time setup (install deps, .env, Nginx, SSL)"
    echo "  --up         Build & start everything"
    echo "  --down       Stop all services"
    echo "  --restart    Restart services"
    echo "  --update     Pull code + rebuild + restart"
    echo "  --logs       Tail live logs"
    echo "  --status     Show service status & health"
    echo "  --backup     Backup data, config, projects"
    echo "  --ssl        Request/renew SSL certificate"
    echo ""
}

case "${1:-}" in
    --setup)    cmd_setup ;;
    --up)       cmd_up ;;
    --down)     cmd_down ;;
    --restart)  cmd_restart ;;
    --update)   cmd_update ;;
    --logs)     cmd_logs ;;
    --status)   cmd_status ;;
    --backup)   cmd_backup ;;
    --ssl)      cmd_ssl ;;
    *)          show_usage ;;
esac
