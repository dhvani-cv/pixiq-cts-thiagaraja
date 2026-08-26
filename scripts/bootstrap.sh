#!/usr/bin/env bash
# ============================================================================
# Bootstrap Script — pixIQ Deployment (Stage 1)
# ============================================================================
#
# Minimal bootstrap script for fresh Jetson devices with no git installed.
# This script can be hosted externally (GitHub Gist, Azure Blob, USB drive).
#
# Usage (online):
#     wget https://gist.github.com/dhvani-cv/{hash}/raw/bootstrap.sh
#     bash bootstrap.sh
#
# Usage (offline USB):
#     bash /media/usb/bootstrap.sh
#
# What this script does:
#     1. Installs minimal prerequisites (git, curl, wget, python3-pip)
#     2. Detects dual NICs (Intel = factory LAN, Realtek = internet) by
#        kernel driver and creates idempotent NetworkManager profiles:
#        • pixiq-profile  — static IP for factory LAN (cameras, PLC, HMI)
#        • internet-profile — DHCP for internet NIC (Azure, SSH, apt)
#        Existing profiles with matching settings are skipped; mismatched
#        ones are updated. Profiles are activated after creation —
#        disconnected cables produce warnings, not failures.
#     3. Guides GitHub SSH key setup and validation
#     4. Clones the repository to $HOME/cone-transport-system-pixiq
#     5. Generates ~/.local/share/pixiq/bootstrap_report.json
#
# This script is IDEMPOTENT — safe to re-run after failures.
# The report is saved under the user’s home directory so re-running
# without sudo never hits permission errors.
#
# ============================================================================

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[Bootstrap]${NC} $*"; }
warn() { echo -e "${YELLOW}[Bootstrap]${NC} $*"; }
err()  { echo -e "${RED}[Bootstrap]${NC} $*" >&2; }
step() { echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${CYAN}▶ $*${NC}"; echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

# ── Configuration ────────────────────────────────────────────────────────────
REPO_URL="git@github.com:dhvani-cv/cone-transport-system-pixiq.git"
REPO_BRANCH="${DEPLOY_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME}"
REPO_DIR="$INSTALL_DIR/cone-transport-system-pixiq"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Report lives in user-writable directory (no root/sudo needed on re-run)
REPORT_DIR="$HOME/.local/share/pixiq"
mkdir -p "$REPORT_DIR" 2>/dev/null || true
REPORT_FILE="$REPORT_DIR/bootstrap_report.json"

# ── Network Profile Configuration ────────────────────────────────────────────
# Factory LAN static IP — change here if the Jetson needs a different address
FACTORY_IP="${FACTORY_IP:-192.168.1.163}"
FACTORY_PREFIX="20"                       # /20 = 255.255.240.0
FACTORY_PROFILE_NAME="pixiq-profile"      # NetworkManager connection name
INTERNET_PROFILE_NAME="internet-profile"  # NetworkManager connection name

# Handle existing report file to prevent permission errors
if [ -f "$REPORT_FILE" ]; then
    log "Removing existing bootstrap report file..."
    rm -f "$REPORT_FILE" 2>/dev/null || true
fi

# ── Phase Tracking ───────────────────────────────────────────────────────────
declare -a PHASES=()
declare -A PHASE_STATUS=()
declare -A PHASE_CHECKS=()
declare -A PHASE_ERRORS=()
declare -A PHASE_WARNINGS=()
declare -A PHASE_DURATION=()

OVERALL_STATUS="success"

add_phase() {
    local phase_name=$1
    PHASES+=("$phase_name")
    PHASE_STATUS["$phase_name"]="not_started"
    PHASE_CHECKS["$phase_name"]=""
    PHASE_ERRORS["$phase_name"]=""
    PHASE_WARNINGS["$phase_name"]=""
}

start_phase() {
    local phase_name=$1
    PHASE_STATUS["$phase_name"]="in_progress"
    PHASE_START_TIME=$(date +%s)
}

end_phase() {
    local phase_name=$1
    local status=$2
    PHASE_STATUS["$phase_name"]="$status"
    
    local phase_end=$(date +%s)
    PHASE_DURATION["$phase_name"]=$((phase_end - PHASE_START_TIME))
    
    if [ "$status" == "failed" ]; then
        OVERALL_STATUS="failed"
    elif [ "$status" == "warning" ] && [ "$OVERALL_STATUS" != "failed" ]; then
        OVERALL_STATUS="warning"
    fi
}

add_check() {
    local phase=$1
    local name=$2
    local status=$3
    local details=${4:-""}
    
    local check_json="{\"name\":\"$name\",\"status\":\"$status\",\"details\":\"$details\"}"
    
    if [ -z "${PHASE_CHECKS[$phase]}" ]; then
        PHASE_CHECKS["$phase"]="$check_json"
    else
        PHASE_CHECKS["$phase"]="${PHASE_CHECKS[$phase]},$check_json"
    fi
}

add_error() {
    local phase=$1
    local message=$2
    
    if [ -z "${PHASE_ERRORS[$phase]}" ]; then
        PHASE_ERRORS["$phase"]="\"$message\""
    else
        PHASE_ERRORS["$phase"]="${PHASE_ERRORS[$phase]},\"$message\""
    fi
}

add_warning() {
    local phase=$1
    local message=$2
    
    if [ -z "${PHASE_WARNINGS[$phase]}" ]; then
        PHASE_WARNINGS["$phase"]="\"$message\""
    else
        PHASE_WARNINGS["$phase"]="${PHASE_WARNINGS[$phase]},\"$message\""
    fi
}

# ── Report Generation ────────────────────────────────────────────────────────
generate_report() {
    local end_time=$(date +%s)
    local end_timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local duration=$((end_time - START_TIME))
    
    # Build network_profiles JSON snippet
    local net_json=""
    if [ -n "${INTEL_NIC:-}" ] || [ -n "${REALTEK_NIC:-}" ]; then
        net_json=$(cat <<NETEOF
  "network_profiles": {
    "factory_lan": {
      "profile": "${FACTORY_PROFILE_NAME}",
      "device": "${INTEL_NIC:-undetected}",
      "ip": "${FACTORY_IP}/${FACTORY_PREFIX}",
      "method": "manual"
    },
    "internet": {
      "profile": "${INTERNET_PROFILE_NAME}",
      "device": "${REALTEK_NIC:-undetected}",
      "method": "auto"
    }
  },
NETEOF
)
    else
        net_json='  "network_profiles": {},'
    fi

    cat > "$REPORT_FILE" <<EOF
{
  "report_type": "bootstrap",
  "report_version": "2.0",
  "deployment_id": "bootstrap_$TIMESTAMP",
  "timestamp_start": "$START_TIMESTAMP",
  "timestamp_end": "$end_timestamp",
  "duration_seconds": $duration,
  "overall_status": "$OVERALL_STATUS",
  "system_info": {
    "hostname": "$(hostname)",
    "architecture": "$(uname -m)"
  },
$net_json
  "phases": [
EOF
    
    local first_phase=true
    for phase in "${PHASES[@]}"; do
        [[ "$first_phase" == false ]] && echo "," >> "$REPORT_FILE"
        first_phase=false
        
        local checks="${PHASE_CHECKS[$phase]:-}"
        local errors="${PHASE_ERRORS[$phase]:-}"
        local warnings="${PHASE_WARNINGS[$phase]:-}"
        local duration="${PHASE_DURATION[$phase]:-0}"
        local status="${PHASE_STATUS[$phase]}"
        
        cat >> "$REPORT_FILE" <<EOF
    {
      "name": "$phase",
      "status": "$status",
      "duration_seconds": $duration,
      "checks": [${checks}],
      "errors": [${errors}],
      "warnings": [${warnings}]
    }
EOF
    done
    
    # Calculate validation summary
    local total_checks=0
    local passed=0
    local failed=0
    local warnings_count=0
    
    for phase in "${PHASES[@]}"; do
        local checks="${PHASE_CHECKS[$phase]}"
        if [ -n "$checks" ]; then
            local phase_total=$(echo "$checks" | grep -o '"status"' | wc -l)
            local phase_passed=$(echo "$checks" | grep -o '"status":"success"' | wc -l)
            local phase_failed=$(echo "$checks" | grep -o '"status":"failed"' | wc -l)
            local phase_warnings=$(echo "$checks" | grep -o '"status":"warning"' | wc -l)
            
            total_checks=$((total_checks + phase_total))
            passed=$((passed + phase_passed))
            failed=$((failed + phase_failed))
            warnings_count=$((warnings_count + phase_warnings))
        fi
    done
    
    cat >> "$REPORT_FILE" <<EOF

  ],
  "validation_summary": {
    "total_checks": $total_checks,
    "passed": $passed,
    "failed": $failed,
    "warnings": $warnings_count
  }
}
EOF
    
    log "Bootstrap report saved: $REPORT_FILE"
}

# ── Summary Display ──────────────────────────────────────────────────────────
# Defined as a function so it can be called from the error path too
show_bootstrap_summary() {
    local end_time=$(date +%s)
    local total_duration=$((end_time - START_TIME))
    
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════════╗${NC}"
    if [ "$OVERALL_STATUS" == "success" ]; then
        echo -e "${CYAN}║${NC}  ${GREEN}✅  BOOTSTRAP COMPLETED SUCCESSFULLY${NC}                              ${CYAN}║${NC}"
    elif [ "$OVERALL_STATUS" == "warning" ]; then
        echo -e "${CYAN}║${NC}  ${YELLOW}⚠️   BOOTSTRAP COMPLETED WITH WARNINGS${NC}                             ${CYAN}║${NC}"
    else
        echo -e "${CYAN}║${NC}  ${RED}❌  BOOTSTRAP FAILED${NC}                                               ${CYAN}║${NC}"
    fi
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BLUE}Elapsed:${NC} ${total_duration}s"
    echo -e "  ${BLUE}Report: ${NC} $REPORT_FILE"
    echo ""
    
    # ── Phase Summary Table ───────────────────────────────────────────────
    echo -e "${CYAN}┌──────────────────────────────┬────────────┬──────────┐${NC}"
    echo -e "${CYAN}│  Phase                       │  Status    │  Time    │${NC}"
    echo -e "${CYAN}├──────────────────────────────┼────────────┼──────────┤${NC}"
    for phase in "${PHASES[@]}"; do
        p_status="${PHASE_STATUS[$phase]}"
        p_dur="${PHASE_DURATION[$phase]:-─}s"
        if [ "$p_status" == "success" ]; then
            STATUS_COL="${GREEN}✓ ok         ${NC}"
        elif [ "$p_status" == "warning" ]; then
            STATUS_COL="${YELLOW}⚠ warn       ${NC}"
        elif [ "$p_status" == "failed" ]; then
            STATUS_COL="${RED}✗ FAILED     ${NC}"
        else
            STATUS_COL="${YELLOW}○ skipped    ${NC}"
        fi
        printf "${CYAN}│${NC}  %-28s ${CYAN}│${NC}  %b${CYAN}│${NC}  %-6s  ${CYAN}│${NC}\n" \
            "$phase" "$STATUS_COL" "$p_dur"
    done
    echo -e "${CYAN}└──────────────────────────────┴────────────┴──────────┘${NC}"
    echo ""
    
    # ── Detailed Check Results ────────────────────────────────────────────
    echo -e "${CYAN}┌─────────────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${CYAN}│                      Detailed Check Results                        │${NC}"
    echo -e "${CYAN}└─────────────────────────────────────────────────────────────────────┘${NC}"
    echo ""
    for phase in "${PHASES[@]}"; do
        p_status="${PHASE_STATUS[$phase]}"
        if [ "$p_status" == "success" ]; then
            ph_icon="${GREEN}✓${NC}"
        elif [ "$p_status" == "warning" ]; then
            ph_icon="${YELLOW}⚠${NC}"
        elif [ "$p_status" == "failed" ]; then
            ph_icon="${RED}✗${NC}"
        else
            ph_icon="${YELLOW}○${NC}"
        fi
        p_dur="${PHASE_DURATION[$phase]:-0}"
        echo -e "  $ph_icon ${BLUE}[$phase]${NC}  ${CYAN}(${p_dur}s)${NC}"
        # Parse checks
        checks_raw="${PHASE_CHECKS[$phase]:-}"
        if [ -n "$checks_raw" ]; then
            while IFS= read -r check_entry; do
                c_name=$(echo "$check_entry"  | grep -oP '"name":\s*"\K[^"]+' || echo "?")
                c_status=$(echo "$check_entry" | grep -oP '"status":\s*"\K[^"]+' || echo "?")
                c_detail=$(echo "$check_entry" | grep -oP '"details":\s*"\K[^"]+' || echo "")
                if [ "$c_status" == "success" ]; then
                    c_icon="${GREEN}✓${NC}"
                elif [ "$c_status" == "warning" ]; then
                    c_icon="${YELLOW}⚠${NC}"
                elif [ "$c_status" == "failed" ]; then
                    c_icon="${RED}✗${NC}"
                else
                    c_icon="${YELLOW}○${NC}"
                fi
                if [ -n "$c_detail" ]; then
                    printf "      %b  %-24s %b\n" "$c_icon" "$c_name" "${CYAN}$c_detail${NC}"
                else
                    printf "      %b  %s\n" "$c_icon" "$c_name"
                fi
            done < <(echo "$checks_raw" | grep -oP '\{[^}]+\}')
        fi
        # Print errors
        if [ -n "${PHASE_ERRORS[$phase]:-}" ]; then
            echo "${PHASE_ERRORS[$phase]}" | grep -oP '"\K[^"]+(?=")' | while read -r emsg; do
                echo -e "      ${RED}└─ ERROR:${NC} $emsg"
            done
        fi
        echo ""
    done
}

# ── Define Phases ────────────────────────────────────────────────────────────
add_phase "privileges"
add_phase "prerequisites"
add_phase "network_profiles"
add_phase "ssh_authentication"
add_phase "repository"

# ── Start Bootstrap ──────────────────────────────────────────────────────────
START_TIME=$(date +%s)
START_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo ""
echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║                                                                ║${NC}"
echo -e "${CYAN}║          pixIQ Bootstrap — Stage 1 Deployment                  ║${NC}"
echo -e "${CYAN}║                                                                ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

log "Repository: $REPO_URL (branch: $REPO_BRANCH)"
log "Install directory: $INSTALL_DIR"
log "Report: $REPORT_FILE"
echo ""

# ============================================================================
# PHASE 1: PRIVILEGES
# ============================================================================

step "Phase 1: Checking Privileges"
start_phase "privileges"

if [ "$EUID" -ne 0 ]; then
    warn "Not running as root. Some operations may require sudo."
    SUDO="sudo"
    add_check "privileges" "root_access" "warning" "running as $(whoami) — using sudo"
else
    log "Running as root ✓"
    SUDO=""
    add_check "privileges" "root_access" "success" "root"
fi

# Architecture check
ARCH=$(uname -m)
add_check "privileges" "architecture" "success" "$ARCH"
log "Architecture: $ARCH"

end_phase "privileges" "success"

# ── Error Trap ────────────────────────────────────────────────────────────────
# Ensure summary + report are always shown, even on unexpected script errors
trap '{
    OVERALL_STATUS="failed"
    generate_report
    show_bootstrap_summary
}' ERR

# ============================================================================
# PHASE 2: PREREQUISITES
# ============================================================================

step "Phase 2: Installing Prerequisites"
start_phase "prerequisites"

# Check and install git
if command -v git &> /dev/null; then
    GIT_VERSION=$(git --version | awk '{print $3}')
    log "git already installed: $GIT_VERSION ✓"
    add_check "prerequisites" "git" "success" "$GIT_VERSION (already installed)"
else
    log "Installing git..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y git
    GIT_VERSION=$(git --version | awk '{print $3}')
    log "git installed: $GIT_VERSION ✓"
    add_check "prerequisites" "git" "success" "$GIT_VERSION"
fi

# Check and install curl
if command -v curl &> /dev/null; then
    CURL_VER=$(curl --version 2>/dev/null | head -n1 | awk '{print $2}')
    log "curl already installed: $CURL_VER ✓"
    add_check "prerequisites" "curl" "success" "$CURL_VER (already installed)"
else
    log "Installing curl..."
    $SUDO apt-get install -y curl
    CURL_VER=$(curl --version 2>/dev/null | head -n1 | awk '{print $2}')
    log "curl installed: $CURL_VER ✓"
    add_check "prerequisites" "curl" "success" "$CURL_VER"
fi

# Check and install wget
if command -v wget &> /dev/null; then
    WGET_VER=$(wget --version 2>/dev/null | head -n1 | awk '{print $3}')
    log "wget already installed: $WGET_VER ✓"
    add_check "prerequisites" "wget" "success" "$WGET_VER (already installed)"
else
    log "Installing wget..."
    $SUDO apt-get install -y wget
    WGET_VER=$(wget --version 2>/dev/null | head -n1 | awk '{print $3}')
    log "wget installed: $WGET_VER ✓"
    add_check "prerequisites" "wget" "success" "$WGET_VER"
fi

# Check and install python3-pip
if command -v pip3 &> /dev/null; then
    PIP_VER=$(pip3 --version 2>/dev/null | awk '{print $2}')
    log "pip3 already installed: $PIP_VER ✓"
    add_check "prerequisites" "pip3" "success" "$PIP_VER (already installed)"
else
    log "Installing python3-pip..."
    $SUDO apt-get install -y python3-pip
    PIP_VER=$(pip3 --version 2>/dev/null | awk '{print $2}')
    log "pip3 installed: $PIP_VER ✓"
    add_check "prerequisites" "pip3" "success" "$PIP_VER"
fi

end_phase "prerequisites" "success"

# ============================================================================
# PHASE 3: NETWORK PROFILES (Dual NIC)
# ============================================================================

step "Phase 3: Network Profiles (Dual NIC)"
start_phase "network_profiles"

NET_PHASE_STATUS="success"

# ── 3a. Detect Intel and Realtek NICs by driver ─────────────────────────────
# Intel drivers: igb, igc, e1000e, ixgbe
# Realtek drivers: r8169, r8168, r8125
detect_nic_by_driver() {
    local target_drivers="$1"  # pipe-separated: "igb|igc|e1000e"
    for iface in /sys/class/net/*/device/driver; do
        [ -e "$iface" ] || continue
        local dev_name
        dev_name=$(echo "$iface" | cut -d'/' -f5)
        # Skip loopback, virtual, docker interfaces
        case "$dev_name" in lo|docker*|veth*|br-*|virbr*) continue ;; esac
        local driver_link
        driver_link=$(readlink -f "$iface" 2>/dev/null)
        local driver_name
        driver_name=$(basename "$driver_link" 2>/dev/null)
        if echo "$driver_name" | grep -qE "^($target_drivers)$"; then
            echo "$dev_name"
            return 0
        fi
    done
    return 1
}

# Detect Intel NIC (factory LAN)
INTEL_NIC=""
if INTEL_NIC=$(detect_nic_by_driver "igb|igc|e1000e|ixgbe|i40e"); then
    log "Intel NIC detected: $INTEL_NIC ✓"
    add_check "network_profiles" "lan1_detect" "success" "Intel on $INTEL_NIC"
else
    warn "Intel NIC not detected by driver — will create profile without device binding"
    add_check "network_profiles" "lan1_detect" "warning" "not auto-detected"
    add_warning "network_profiles" "Intel NIC not detected — assign device manually via nmcli"
fi

# Detect Realtek NIC (internet)
REALTEK_NIC=""
if REALTEK_NIC=$(detect_nic_by_driver "r8169|r8168|r8125"); then
    log "Realtek NIC detected: $REALTEK_NIC ✓"
    add_check "network_profiles" "lan2_detect" "success" "Realtek on $REALTEK_NIC"
else
    warn "Realtek NIC not detected by driver — will create profile without device binding"
    add_check "network_profiles" "lan2_detect" "warning" "not auto-detected"
    add_warning "network_profiles" "Realtek NIC not detected — assign device manually via nmcli"
fi

# ── 3b. Helper: Create or update a NetworkManager connection ────────────────
# Usage: ensure_nm_profile <name> <method> <device> [ip] [prefix]
#   method = "manual" or "auto"
#   device = NIC name or "" for no device binding
#   ip/prefix only used when method=manual
ensure_nm_profile() {
    local name="$1"
    local method="$2"
    local device="$3"
    local ip="${4:-}"
    local prefix="${5:-}"

    local needs_create=false
    local needs_update=false

    if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$name"; then
        # Profile exists — check if settings match
        local cur_method cur_ip cur_dev
        cur_method=$(nmcli -g ipv4.method connection show "$name" 2>/dev/null)
        cur_ip=$(nmcli -g ipv4.addresses connection show "$name" 2>/dev/null)
        cur_dev=$(nmcli -g connection.interface-name connection show "$name" 2>/dev/null)

        if [ "$method" = "manual" ]; then
            local expected_ip="${ip}/${prefix}"
            if [ "$cur_method" = "manual" ] && [ "$cur_ip" = "$expected_ip" ]; then
                if [ -z "$device" ] || [ "$cur_dev" = "$device" ]; then
                    log "  Profile '$name' already configured correctly — skipping" >&2
                    echo "skipped"
                    return 0
                fi
            fi
        else
            if [ "$cur_method" = "auto" ]; then
                if [ -z "$device" ] || [ "$cur_dev" = "$device" ]; then
                    log "  Profile '$name' already configured correctly — skipping" >&2
                    echo "skipped"
                    return 0
                fi
            fi
        fi

        # Settings mismatch — delete and recreate
        log "  Profile '$name' exists but settings differ — updating..." >&2
        nmcli connection delete "$name" >/dev/null 2>&1 || true
        needs_create=true
        needs_update=true
    else
        needs_create=true
    fi

    if [ "$needs_create" = true ]; then
        local nmcli_args=(connection add con-name "$name" type ethernet autoconnect yes)

        # Bind to device if detected
        if [ -n "$device" ]; then
            nmcli_args+=(ifname "$device")
        else
            nmcli_args+=(ifname "*")
        fi

        if [ "$method" = "manual" ]; then
            nmcli_args+=(ipv4.method manual ipv4.addresses "${ip}/${prefix}")
            # No default gateway for factory LAN — only the internet NIC should have one
            nmcli_args+=(ipv4.gateway "")
            # Disable IPv6 on factory LAN (cameras are IPv4 only)
            nmcli_args+=(ipv6.method disabled)
        else
            nmcli_args+=(ipv4.method auto)
        fi

        if $SUDO nmcli "${nmcli_args[@]}" >/dev/null 2>&1; then
            if [ "$needs_update" = true ]; then
                echo "updated"
            else
                echo "created"
            fi
            return 0
        else
            echo "failed"
            return 1
        fi
    fi
}

# ── 3c. Create pixiq-profile (factory LAN — static IP) ──────────────────────
log "Configuring '$FACTORY_PROFILE_NAME' (factory LAN, static ${FACTORY_IP}/${FACTORY_PREFIX})..."
PIXIQ_RESULT=$(ensure_nm_profile "$FACTORY_PROFILE_NAME" "manual" "$INTEL_NIC" "$FACTORY_IP" "$FACTORY_PREFIX")
case "$PIXIQ_RESULT" in
    skipped)
        log "  $FACTORY_PROFILE_NAME: already configured ✓"
        add_check "network_profiles" "lan1_profile" "success" "$FACTORY_PROFILE_NAME (${FACTORY_IP}/${FACTORY_PREFIX}, already configured)"
        ;;
    created)
        log "  $FACTORY_PROFILE_NAME: created ✓"
        add_check "network_profiles" "lan1_profile" "success" "$FACTORY_PROFILE_NAME (${FACTORY_IP}/${FACTORY_PREFIX}, created)"
        ;;
    updated)
        log "  $FACTORY_PROFILE_NAME: updated ✓"
        add_check "network_profiles" "lan1_profile" "success" "$FACTORY_PROFILE_NAME (${FACTORY_IP}/${FACTORY_PREFIX}, updated)"
        ;;
    failed|*)
        err "  $FACTORY_PROFILE_NAME: creation failed"
        add_check "network_profiles" "lan1_profile" "failed" "$FACTORY_PROFILE_NAME — nmcli error"
        add_error "network_profiles" "Failed to create $FACTORY_PROFILE_NAME profile"
        NET_PHASE_STATUS="failed"
        ;;
esac

# ── 3d. Create internet-profile (internet — DHCP) ───────────────────────────
log "Configuring '$INTERNET_PROFILE_NAME' (internet, DHCP)..."
INET_RESULT=$(ensure_nm_profile "$INTERNET_PROFILE_NAME" "auto" "$REALTEK_NIC")
case "$INET_RESULT" in
    skipped)
        log "  $INTERNET_PROFILE_NAME: already configured ✓"
        add_check "network_profiles" "lan2_profile" "success" "$INTERNET_PROFILE_NAME (DHCP, already configured)"
        ;;
    created)
        log "  $INTERNET_PROFILE_NAME: created ✓"
        add_check "network_profiles" "lan2_profile" "success" "$INTERNET_PROFILE_NAME (DHCP, created)"
        ;;
    updated)
        log "  $INTERNET_PROFILE_NAME: updated ✓"
        add_check "network_profiles" "lan2_profile" "success" "$INTERNET_PROFILE_NAME (DHCP, updated)"
        ;;
    failed|*)
        err "  $INTERNET_PROFILE_NAME: creation failed"
        add_check "network_profiles" "lan2_profile" "failed" "$INTERNET_PROFILE_NAME — nmcli error"
        add_error "network_profiles" "Failed to create $INTERNET_PROFILE_NAME profile"
        NET_PHASE_STATUS="failed"
        ;;
esac

# ── 3e. Activate profiles ────────────────────────────────────────────────────
# Bring up the newly created/updated profiles.  If a cable is not
# plugged in, nmcli will fail — that's fine, the profile is saved and
# will auto-activate once the cable is connected.
log "Activating network profiles..."

for _prof_name in "$FACTORY_PROFILE_NAME" "$INTERNET_PROFILE_NAME"; do
    # Skip activation if the profile was never created (failed earlier)
    if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$_prof_name"; then
        warn "  $_prof_name: profile not found — skipping activation"
        continue
    fi

    if $SUDO nmcli connection up "$_prof_name" 2>/dev/null; then
        log "  $_prof_name: activated ✓"
        add_check "network_profiles" "activate_${_prof_name}" "success" "profile up"
    else
        # Distinguish "no cable" from other errors
        _dev=""
        _dev=$(nmcli -g connection.interface-name connection show "$_prof_name" 2>/dev/null || true)
        if [ -n "$_dev" ] && [ "$_dev" != "*" ]; then
            _carrier=$(cat "/sys/class/net/$_dev/carrier" 2>/dev/null || echo "0")
            if [ "$_carrier" = "0" ]; then
                warn "  $_prof_name: cable not connected on $_dev — profile saved, will auto-activate when plugged in"
                add_check "network_profiles" "activate_${_prof_name}" "warning" "no cable on $_dev — profile saved"
                add_warning "network_profiles" "$_prof_name: cable not connected on $_dev"
                [ "$NET_PHASE_STATUS" != "failed" ] && NET_PHASE_STATUS="warning"
            else
                warn "  $_prof_name: activation failed on $_dev (carrier present)"
                add_check "network_profiles" "activate_${_prof_name}" "warning" "activation failed on $_dev"
                add_warning "network_profiles" "$_prof_name: activation failed despite carrier on $_dev"
                [ "$NET_PHASE_STATUS" != "failed" ] && NET_PHASE_STATUS="warning"
            fi
        else
            warn "  $_prof_name: activation failed (device not bound or not detected)"
            add_check "network_profiles" "activate_${_prof_name}" "warning" "no device bound"
            add_warning "network_profiles" "$_prof_name: could not activate — no device bound"
            [ "$NET_PHASE_STATUS" != "failed" ] && NET_PHASE_STATUS="warning"
        fi
    fi
done

# ── 3f. Network validation — report NIC link state ───────────────────────────
log "Validating network link states..."
for _nic_name in "$INTEL_NIC" "$REALTEK_NIC"; do
    [ -z "$_nic_name" ] && continue
    _link_state=$(cat "/sys/class/net/$_nic_name/operstate" 2>/dev/null || echo "unknown")
    _mtu=$(cat "/sys/class/net/$_nic_name/mtu" 2>/dev/null || echo "?")
    if [ "$_link_state" = "up" ]; then
        log "  $_nic_name: link UP (MTU $_mtu) ✓"
        add_check "network_profiles" "link_${_nic_name}" "success" "link up, MTU $_mtu"
    else
        warn "  $_nic_name: link $_link_state (MTU $_mtu) — cable may not be connected"
        add_check "network_profiles" "link_${_nic_name}" "warning" "link $_link_state, MTU $_mtu"
        [ "$NET_PHASE_STATUS" != "failed" ] && NET_PHASE_STATUS="warning"
    fi
done

end_phase "network_profiles" "$NET_PHASE_STATUS"

# ============================================================================
# PHASE 4: SSH AUTHENTICATION
# ============================================================================

step "Phase 4: GitHub SSH Authentication"
start_phase "ssh_authentication"

SSH_KEY_PATH="$HOME/.ssh/id_ed25519"

if [ -f "$SSH_KEY_PATH" ]; then
    log "SSH key already exists: $SSH_KEY_PATH ✓"
    if ssh-keygen -l -f "$SSH_KEY_PATH" &>/dev/null; then
        SSH_FINGERPRINT=$(ssh-keygen -l -f "$SSH_KEY_PATH" | awk '{print $2}')
        log "Key fingerprint: $SSH_FINGERPRINT"
        add_check "ssh_authentication" "ssh_key" "success" "exists ($SSH_FINGERPRINT)"
    else
        add_check "ssh_authentication" "ssh_key" "success" "exists at $SSH_KEY_PATH"
    fi
else
    warn "No SSH key found. Generating new SSH key..."
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    
    ssh-keygen -t ed25519 -f "$SSH_KEY_PATH" -N "" -C "sieger-pixiq-$(hostname)"
    
    log "SSH key generated: $SSH_KEY_PATH ✓"
    add_check "ssh_authentication" "ssh_key" "success" "generated"
fi

# Start ssh-agent and add key to agent
log "Starting ssh-agent and adding key..."
eval "$(ssh-agent -s)" > /dev/null
ssh-add "$SSH_KEY_PATH" 2>&1 > /dev/null
log "Key added to ssh-agent ✓"
add_check "ssh_authentication" "ssh_agent" "success" "key added"

# Display public key for GitHub
echo ""
warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
warn "ACTION REQUIRED: Add this SSH key to GitHub"
warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
cat "$SSH_KEY_PATH.pub"
echo ""
warn "1. Copy the key above"
warn "2. Go to: https://github.com/settings/ssh/new"
warn "3. Paste the key and save"
echo ""
read -p "Press ENTER after adding the key to GitHub..." </dev/tty

# Validate SSH access to GitHub
log "Validating GitHub SSH access..."
if git ls-remote "$REPO_URL" HEAD &>/dev/null; then
    log "GitHub SSH authentication successful ✓"
    add_check "ssh_authentication" "github_auth" "success" "authenticated"
else
    err "GitHub SSH authentication failed"
    err "Please ensure your SSH key is added to GitHub"
    add_check "ssh_authentication" "github_auth" "failed" "authentication failed"
    add_error "ssh_authentication" "GitHub SSH authentication failed"
    end_phase "ssh_authentication" "failed"
    
    generate_report
    show_bootstrap_summary
    exit 1
fi

end_phase "ssh_authentication" "success"

# ============================================================================
# PHASE 5: REPOSITORY CLONE
# ============================================================================

step "Phase 5: Cloning Repository"
start_phase "repository"

# Create install directory
if [ ! -d "$INSTALL_DIR" ]; then
    log "Creating install directory: $INSTALL_DIR"
    $SUDO mkdir -p "$INSTALL_DIR"
    $SUDO chown -R $(whoami):$(whoami) "$INSTALL_DIR"
    add_check "repository" "install_dir" "success" "created $INSTALL_DIR"
else
    add_check "repository" "install_dir" "success" "exists $INSTALL_DIR"
fi

# Clone or update repository
if [ -d "$REPO_DIR/.git" ]; then
    log "Repository already exists, updating..."
    cd "$REPO_DIR"
    
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    if [ "$CURRENT_BRANCH" != "$REPO_BRANCH" ]; then
        warn "Switching from branch '$CURRENT_BRANCH' to '$REPO_BRANCH'"
        git fetch origin
        git checkout "$REPO_BRANCH"
    fi
    
    git pull origin "$REPO_BRANCH"
    COMMIT=$(git rev-parse --short HEAD)
    log "Repository updated to: $COMMIT ✓"
    add_check "repository" "repo_clone" "success" "updated — branch: $REPO_BRANCH, commit: $COMMIT"
else
    log "Cloning repository..."
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
    
    cd "$REPO_DIR"
    COMMIT=$(git rev-parse --short HEAD)
    log "Repository cloned successfully ✓"
    add_check "repository" "repo_clone" "success" "cloned — branch: $REPO_BRANCH, commit: $COMMIT"
    
    # Ensure proper ownership of cloned repository
    if [ -n "${SUDO_USER:-}" ] && [ "$EUID" -eq 0 ]; then
        log "Setting repository ownership to $SUDO_USER..."
        chown -R "$SUDO_USER:$SUDO_USER" "$REPO_DIR"
    fi
fi

# Validate key files exist in the cloned repo
if [ -f "$REPO_DIR/scripts/deploy.sh" ]; then
    add_check "repository" "deploy_script" "success" "scripts/deploy.sh found"
else
    add_check "repository" "deploy_script" "failed" "scripts/deploy.sh missing"
    add_error "repository" "Main deployment script not found in repository"
fi

if [ -f "$REPO_DIR/pyproject.toml" ]; then
    add_check "repository" "pyproject" "success" "pyproject.toml found"
else
    add_check "repository" "pyproject" "warning" "pyproject.toml missing"
fi

end_phase "repository" "success"

# ============================================================================
# GENERATE REPORT & SHOW SUMMARY
# ============================================================================

generate_report
show_bootstrap_summary

# ── Next Steps ────────────────────────────────────────────────────────────────
if [ "$OVERALL_STATUS" == "success" ]; then
    echo -e "${GREEN}Next step: Run the main deployment script${NC}"
    echo ""
    echo "  cd $REPO_DIR"
    echo "  sudo ./scripts/deploy.sh"
    echo ""
fi

exit 0