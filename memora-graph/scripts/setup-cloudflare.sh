#!/bin/bash
# Setup script for deploying Memora Graph to Cloudflare
# This script automates the deployment of:
# - D1 database
# - Durable Object Worker (WebSocket sync)
# - Pages site (Graph UI)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
WORKER_DIR="$PROJECT_DIR/worker"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_step() {
    echo -e "\n${BLUE}==>${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}!${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    print_step "Checking prerequisites..."

    if ! command -v node &> /dev/null; then
        print_error "Node.js is not installed. Please install Node.js first."
        exit 1
    fi
    print_success "Node.js found: $(node --version)"

    if ! command -v npm &> /dev/null; then
        print_error "npm is not installed. Please install npm first."
        exit 1
    fi
    print_success "npm found: $(npm --version)"

    # Check if wrangler is available (globally or locally)
    if ! npx wrangler --version &> /dev/null; then
        print_warning "Wrangler not found, will install locally"
    else
        print_success "Wrangler found: $(npx wrangler --version 2>/dev/null | head -1)"
    fi
}

# Install dependencies
install_dependencies() {
    print_step "Installing dependencies..."

    cd "$PROJECT_DIR"
    if [ ! -d "node_modules" ]; then
        npm install
    else
        print_success "Dependencies already installed"
    fi

    cd "$WORKER_DIR"
    if [ ! -d "node_modules" ]; then
        npm install
    else
        print_success "Worker dependencies already installed"
    fi
}

# Check Cloudflare login
check_cloudflare_login() {
    print_step "Checking Cloudflare authentication..."

    cd "$PROJECT_DIR"
    if ! npx wrangler whoami &> /dev/null; then
        print_warning "Not logged into Cloudflare. Starting login..."
        npx wrangler login
    else
        ACCOUNT=$(npx wrangler whoami 2>&1 | grep -oP '(?<=account: ).*' || echo "unknown")
        print_success "Logged into Cloudflare"
    fi
}

# Create D1 database
setup_d1_database() {
    print_step "Setting up D1 database..."

    cd "$PROJECT_DIR"

    # Check if database exists
    DB_EXISTS=$(npx wrangler d1 list 2>/dev/null | grep -c "memora-graph" || true)

    if [ "$DB_EXISTS" -eq 0 ]; then
        print_warning "Creating D1 database 'memora-graph'..."
        DB_OUTPUT=$(npx wrangler d1 create memora-graph 2>&1)
        echo "$DB_OUTPUT"

        # Extract database ID
        DB_ID=$(echo "$DB_OUTPUT" | grep -oP 'database_id = "\K[^"]+' || true)

        if [ -n "$DB_ID" ]; then
            print_success "Database created with ID: $DB_ID"

            # Update wrangler.toml with the new database ID
            if grep -q "database_id" "$PROJECT_DIR/wrangler.toml"; then
                sed -i "s/database_id = \"[^\"]*\"/database_id = \"$DB_ID\"/" "$PROJECT_DIR/wrangler.toml"
                print_success "Updated wrangler.toml with database ID"
            fi
        fi
    else
        print_success "D1 database 'memora-graph' already exists"
    fi

    # Remote D1 migrations are RETIRED: memora-all on deploy-host is the only D1
    # writer (docs/local-primary-implementation.md §0 P6, §6 F3). The setup
    # stops here rather than write D1.
    print_error "Remote D1 migrations are retired: memora-all on deploy-host is the only D1 writer. See docs/local-primary-implementation.md §0 P6 and §6 F3."
    exit 1
}

# Deploy Durable Object Worker
deploy_worker() {
    print_step "Deploying WebSocket Worker (Durable Object)..."

    cd "$WORKER_DIR"

    # Deploy
    WORKER_OUTPUT=$(npx wrangler deploy 2>&1)
    echo "$WORKER_OUTPUT"

    # Extract worker URL
    WORKER_URL=$(echo "$WORKER_OUTPUT" | grep -oP 'https://[^\s]+\.workers\.dev' | head -1 || true)

    if [ -n "$WORKER_URL" ]; then
        print_success "Worker deployed to: $WORKER_URL"

        # Update wrangler.toml with worker URL
        if grep -q "WS_WORKER_URL" "$PROJECT_DIR/wrangler.toml"; then
            sed -i "s|WS_WORKER_URL = \"[^\"]*\"|WS_WORKER_URL = \"$WORKER_URL\"|" "$PROJECT_DIR/wrangler.toml"
            print_success "Updated wrangler.toml with worker URL"
        fi

        # Update cloud_sync.py with worker URL
        CLOUD_SYNC_FILE="$PROJECT_DIR/../memora/cloud_sync.py"
        if [ -f "$CLOUD_SYNC_FILE" ]; then
            sed -i "s|https://memora-graph-sync\.[^\"]*\.workers\.dev|$WORKER_URL|" "$CLOUD_SYNC_FILE"
            print_success "Updated cloud_sync.py with worker URL"
        fi

        echo "$WORKER_URL" > "$PROJECT_DIR/.worker-url"
    else
        print_error "Could not extract worker URL from output"
    fi
}

# Create Pages project
setup_pages_project() {
    print_step "Setting up Cloudflare Pages project..."

    cd "$PROJECT_DIR"

    # Check if project exists
    PROJECT_EXISTS=$(npx wrangler pages project list 2>/dev/null | grep -c "memora-graph" || true)

    if [ "$PROJECT_EXISTS" -eq 0 ]; then
        print_warning "Creating Pages project 'memora-graph'..."
        npx wrangler pages project create memora-graph --production-branch=main 2>/dev/null || true
        print_success "Pages project created"
    else
        print_success "Pages project 'memora-graph' already exists"
    fi
}

# Deploy Pages
deploy_pages() {
    print_step "Deploying to Cloudflare Pages..."

    cd "$PROJECT_DIR"

    # Update frontend with worker URL if available
    if [ -f "$PROJECT_DIR/.worker-url" ]; then
        WORKER_URL=$(cat "$PROJECT_DIR/.worker-url")
        WS_URL=$(echo "$WORKER_URL" | sed 's|https://|wss://|')
        sed -i "s|wss://memora-graph-sync\.[^/]*\.workers\.dev|$WS_URL|" "$PROJECT_DIR/public/index.html"
        print_success "Updated frontend WebSocket URL"
    fi

    # Deploy. The D1 write guard runs first and refuses on any finding
    # (docs/local-primary-implementation.md §0 P6): until the viewer is
    # read-only (slice L7) it fails, so this cannot republish write handlers.
    python3 "$PROJECT_DIR/../scripts/d1_write_guard.py" --scope all || { print_error "D1 write guard failed; refusing to deploy Pages."; exit 1; }; PAGES_OUTPUT=$(npx wrangler pages deploy public --project-name=memora-graph 2>&1)
    echo "$PAGES_OUTPUT"

    # Extract pages URL
    PAGES_URL=$(echo "$PAGES_OUTPUT" | grep -oP 'https://[^\s]+\.pages\.dev' | head -1 || true)

    if [ -n "$PAGES_URL" ]; then
        print_success "Pages deployed to: $PAGES_URL"
        echo "$PAGES_URL" > "$PROJECT_DIR/.pages-url"
    fi
}

# Configure bindings via API
configure_bindings() {
    print_step "Configuring D1 and R2 bindings..."

    print_warning "Bindings need to be configured in Cloudflare Dashboard:"
    echo ""
    echo "  1. Go to: https://dash.cloudflare.com/"
    echo "  2. Navigate to: Workers & Pages > memora-graph > Settings > Bindings"
    echo "  3. Add D1 binding:"
    echo "     - Variable name: DB"
    echo "     - Database: memora-graph"
    echo "  4. Add R2 binding:"
    echo "     - Variable name: R2"
    echo "     - Bucket: memora"
    echo ""

    read -p "Press Enter once you've configured the bindings (or 's' to skip): " response
    if [ "$response" != "s" ]; then
        print_success "Bindings configured"
    else
        print_warning "Skipped binding configuration - you'll need to do this manually"
    fi
}

# Initial sync
run_initial_sync() {
    print_step "Running initial data sync..."

    # Check if .mcp.json exists for credentials
    MCP_JSON="$PROJECT_DIR/../.mcp.json"
    if [ ! -f "$MCP_JSON" ]; then
        print_warning "No .mcp.json found - skipping initial sync"
        print_warning "Run 'npm run sync-remote' after configuring .mcp.json"
        return
    fi

    cd "$PROJECT_DIR"
    bash "$SCRIPT_DIR/sync.sh" --remote
    print_success "Initial sync completed"
}

# Print summary
print_summary() {
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Memora Graph Setup Complete!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""

    if [ -f "$PROJECT_DIR/.pages-url" ]; then
        echo -e "  Graph UI:     $(cat "$PROJECT_DIR/.pages-url")"
    fi

    if [ -f "$PROJECT_DIR/.worker-url" ]; then
        echo -e "  WebSocket:    $(cat "$PROJECT_DIR/.worker-url")"
    fi

    echo ""
    echo "  To enable auto-sync, add to your .mcp.json env:"
    echo "    \"MEMORA_CLOUD_GRAPH_ENABLED\": \"true\""
    echo ""
    echo "  To manually sync:"
    echo "    cd memora-graph && npm run sync-remote"
    echo ""
}

# Main
main() {
    echo ""
    echo -e "${BLUE}Memora Graph - Cloudflare Setup${NC}"
    echo "================================="

    check_prerequisites
    # wrangler.toml is the operator's and git-ignored (CFG1): start from the template.
    if [ ! -f "$PROJECT_DIR/wrangler.toml" ]; then
        cp "$PROJECT_DIR/wrangler.toml.example" "$PROJECT_DIR/wrangler.toml"
        print_success "Created wrangler.toml from wrangler.toml.example (fill in your ids)"
    fi
    install_dependencies
    check_cloudflare_login
    setup_d1_database
    deploy_worker
    setup_pages_project
    deploy_pages
    configure_bindings
    run_initial_sync
    print_summary
}

# Run
main "$@"
