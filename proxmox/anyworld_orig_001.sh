#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)
# Build framework: Copyright (c) 2021-2026 tteck / community-scripts ORG
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Base: https://github.com/community-scripts/ProxmoxVE/blob/main/ct/debian.sh
# App:  https://github.com/GrimmKaos/AnyWorld
#
# Creates a Debian 13 LXC, installs AnyWorld (AI dungeon-master multiplayer
# text RPG), walks through an interactive config.yaml wizard, runs the
# INSTALL.md quality checks and installs a systemd service.
#
# Run on the Proxmox VE host as root:
#   bash anyworld.sh
# Optional overrides:
#   AW_REPO=https://github.com/<fork>/AnyWorld.git AW_BRANCH=main bash anyworld.sh
#
# Update an existing install (run INSIDE the container):
#   anyworld-update            (or re-run this script inside the CT)

APP="AnyWorld"
var_tags="${var_tags:-game;ai}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-2048}"
var_disk="${var_disk:-8}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_arm64="${var_arm64:-yes}"
var_unprivileged="${var_unprivileged:-1}"

AW_REPO="${AW_REPO:-https://github.com/GrimmKaos/AnyWorld.git}"
AW_BRANCH="${AW_BRANCH:-main}"
AW_BT="AnyWorld Setup"

header_info "$APP"
variables
# AnyWorld has no installer in the community-scripts repo. Build the container
# with the stock Debian installer, then layer AnyWorld on top (aw_install).
var_install="debian-install"
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  if [[ ! -d /opt/anyworld/.git ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi
  msg_info "Updating Debian packages"
  $STD apt update
  $STD apt -y upgrade
  msg_ok "Updated Debian packages"
  msg_info "Updating ${APP}"
  $STD /usr/local/bin/anyworld-update
  msg_ok "Updated ${APP} (the restart ends any live game session)"
  exit
}

# ==============================================================================
# Wizard helpers (run on the Proxmox host)
# ==============================================================================
AW_TMP=""
CT_IP=""

aw_cleanup() {
  if [[ -n "$AW_TMP" && -d "$AW_TMP" ]]; then rm -rf "$AW_TMP"; fi
  if [[ -n "${CTID:-}" ]]; then
    pct exec "$CTID" -- rm -f /root/anyworld-answers.env /root/aw-build-config.py >/dev/null 2>&1 || true
  fi
}

aw_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

aw_abort() {
  local choice=""
  choice=$(whiptail --backtitle "$AW_BT" --title "Leave setup?" \
    --menu "Container ${CTID} has already been created.\nWhat would you like to do?" 15 74 3 \
    "back" "Return to the setup wizard" \
    "keep" "Exit and keep the container (AnyWorld NOT installed)" \
    "destroy" "Exit and destroy container ${CTID}" \
    3>&1 1>&2 2>&3) || choice="back"
  case "$choice" in
  keep)
    aw_cleanup
    msg_warn "Setup aborted. CT ${CTID} kept as a plain Debian container."
    exit 0
    ;;
  destroy)
    aw_cleanup
    msg_info "Destroying CT ${CTID}"
    pct stop "$CTID" >/dev/null 2>&1 || true
    pct destroy "$CTID" --purge >/dev/null 2>&1 || true
    msg_ok "Destroyed CT ${CTID}"
    exit 0
    ;;
  *) return 0 ;;
  esac
}

aw_err() { whiptail --backtitle "$AW_BT" --title "Invalid value" --msgbox "$1" 12 72; }

# --- validators: return 0 if valid, show message and return 1 otherwise -------
v_nonempty() { if [[ -n "$1" ]]; then return 0; fi; aw_err "This value is required."; return 1; }
v_nospace() {
  if [[ -n "$1" && "$1" != *[[:space:]]* ]]; then return 0; fi
  aw_err "Required, and may not contain spaces."
  return 1
}
v_port() {
  if [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)); then return 0; fi
  aw_err "Port must be a number between 1 and 65535."
  return 1
}
v_players() {
  if [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 100)); then return 0; fi
  aw_err "Max players must be 1-100 (the limit includes the host)."
  return 1
}
v_ctx() {
  if [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1024)); then return 0; fi
  aw_err "Context window must be a whole number of tokens (>= 1024)."
  return 1
}
v_url() {
  if [[ "$1" =~ ^https?://[^[:space:]]+$ ]]; then return 0; fi
  aw_err "Enter a full URL, e.g. http://192.168.1.20:8033/v1"
  return 1
}
v_opt_posint() {
  if [[ -z "$1" ]] || { [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1)); }; then return 0; fi
  aw_err "Enter a positive whole number, or leave blank for the default."
  return 1
}
v_opt_nonneg() {
  if [[ -z "$1" || "$1" =~ ^[0-9]+$ ]]; then return 0; fi
  aw_err "Enter a whole number (0 or more), or leave blank for the default."
  return 1
}
v_opt_float() {
  if [[ -z "$1" || "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]; then return 0; fi
  aw_err "Enter a number (e.g. 120 or 120.0), or leave blank for the default."
  return 1
}
v_opt_fraction() {
  if [[ -z "$1" ]]; then return 0; fi
  if [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v x="$1" 'BEGIN{exit !(x>=0.5 && x<=1.0)}'; then return 0; fi
  aw_err "Compaction target fraction must be between 0.5 and 1.0."
  return 1
}
v_opt_history() {
  if [[ -z "$1" ]] || { [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 2 && 10#$1 <= 100)); }; then return 0; fi
  aw_err "History round limit must be 2-100, or blank to disable."
  return 1
}
v_tls() {
  local a list
  IFS=',' read -r -a list <<<"$1"
  if ((${#list[@]} == 0)); then aw_err "Enter at least one IP or hostname."; return 1; fi
  for a in "${list[@]}"; do
    a="$(aw_trim "$a")"
    if [[ ! "$a" =~ ^[A-Za-z0-9.:-]+$ ]]; then
      aw_err "Invalid entry: '${a}'\n\nUse comma-separated IPs or hostnames only."
      return 1
    fi
  done
  return 0
}

# aw_input <var> <title> <text> <default> [validator]
aw_input() {
  local __var="$1" __title="$2" __text="$3" __def="$4" __val="${5:-}" __res=""
  while true; do
    if ! __res=$(whiptail --backtitle "$AW_BT" --title "$__title" \
      --inputbox "$__text" 20 78 "$__def" 3>&1 1>&2 2>&3); then
      aw_abort
      continue
    fi
    __res="$(aw_trim "$__res")"
    if [[ -n "$__val" ]] && ! "$__val" "$__res"; then
      __def="$__res"
      continue
    fi
    printf -v "$__var" '%s' "$__res"
    return 0
  done
}

# aw_menu <var> <title> <text> <default-tag> <tag> <desc> [<tag> <desc> ...]
aw_menu() {
  local __var="$1" __title="$2" __text="$3" __def="$4" __res=""
  shift 4
  while true; do
    if __res=$(whiptail --backtitle "$AW_BT" --title "$__title" --default-item "$__def" \
      --menu "$__text" 22 78 6 "$@" 3>&1 1>&2 2>&3); then
      printf -v "$__var" '%s' "$__res"
      return 0
    fi
    aw_abort
  done
}

# aw_yesno <var> <title> <text> <default yes|no>  -> sets var to yes/no
aw_yesno() {
  local __var="$1" __title="$2" __text="$3" __def="${4:-yes}" __rc=0
  local -a __flag=()
  if [[ "$__def" == "no" ]]; then __flag=(--defaultno); fi
  while true; do
    __rc=0
    whiptail --backtitle "$AW_BT" --title "$__title" "${__flag[@]}" \
      --yesno "$__text" 18 78 || __rc=$?
    case "$__rc" in
    0) printf -v "$__var" 'yes'; return 0 ;;
    1) printf -v "$__var" 'no'; return 0 ;;
    *) aw_abort ;;
    esac
  done
}

# aw_secret <var> <title> <label> <allow-generate yes|no>
# Sets <var> and <var>_GEN (yes when auto-generated).
aw_secret() {
  local __var="$1" __title="$2" __label="$3" __gen="${4:-no}" __p1="" __p2="" __hint=""
  if [[ "$__gen" == "yes" ]]; then __hint="\n\nLeave blank to auto-generate a random password."; fi
  while true; do
    if ! __p1=$(whiptail --backtitle "$AW_BT" --title "$__title" \
      --passwordbox "Enter the ${__label}.${__hint}" 14 78 3>&1 1>&2 2>&3); then
      aw_abort
      continue
    fi
    if [[ -z "$__p1" && "$__gen" == "yes" ]]; then
      printf -v "$__var" '%s' "$(openssl rand -hex 12)"
      printf -v "${__var}_GEN" 'yes'
      return 0
    fi
    if [[ -z "$__p1" ]]; then aw_err "The ${__label} cannot be empty."; continue; fi
    if [[ "$__p1" == *"'"* || "$__p1" == *\\* ]]; then
      aw_err "The ${__label} may not contain single quotes (') or backslashes (\\)."
      continue
    fi
    if ! __p2=$(whiptail --backtitle "$AW_BT" --title "$__title" \
      --passwordbox "Confirm the ${__label}." 10 78 3>&1 1>&2 2>&3); then
      continue
    fi
    if [[ "$__p1" != "$__p2" ]]; then aw_err "The values do not match. Please try again."; continue; fi
    printf -v "$__var" '%s' "$__p1"
    printf -v "${__var}_GEN" 'no'
    return 0
  done
}

aw_get_ct_ip() {
  local i
  for i in $(seq 1 20); do
    CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')" || CT_IP=""
    if [[ -n "$CT_IP" ]]; then return 0; fi
    sleep 1
  done
  CT_IP=""
}

# ==============================================================================
# Interactive config.yaml wizard
# ==============================================================================
aw_wizard() {
  AW_HOST_PW_GEN="no"
  AW_PLAYER_PW_GEN="no"

  whiptail --backtitle "$AW_BT" --title "AnyWorld configuration" --msgbox \
    "Container ${CTID} is ready. This wizard builds AnyWorld's config.yaml.\n\nRequired: game port, host/player passwords, AI backend, TLS addresses.\nOptional: advanced tuning, quality checks, service, benchmark.\n\nBlank optional fields keep AnyWorld's built-in defaults.\nCancel/ESC at any prompt lets you go back or abort." 16 78

  # --- Server ------------------------------------------------------------------
  aw_input AW_PORT "Game server port" \
    "TCP port for the game (HTTPS only).\nPlayers will open https://<container-ip>:<port>/\n\nDefault: 4141" \
    "${AW_PORT:-4141}" v_port
  aw_input AW_MAX_PLAYERS "Maximum players" \
    "Maximum number of players per game.\nThe limit INCLUDES the host.\n\nDefault: 6" \
    "${AW_MAX_PLAYERS:-6}" v_players

  # --- Passwords (required, distinct) -------------------------------------------
  aw_secret AW_HOST_PW "Host password" \
    "HOST password (the host signs in first and writes the scenario)" yes
  while true; do
    aw_secret AW_PLAYER_PW "Player password" \
      "PLAYER password (shared with players to join)" yes
    if [[ "$AW_PLAYER_PW" != "$AW_HOST_PW" ]]; then break; fi
    aw_err "Host and player passwords must be different (AnyWorld rejects identical passwords)."
  done
  aw_menu AW_SECRET_STORE "Password storage" \
    "Where should the passwords be stored?\n\nAnyWorld's docs recommend keeping secrets in .env or the process environment rather than config.yaml. Both files are chmod 600 either way." \
    "${AW_SECRET_STORE:-env}" \
    "env" ".env as AD_SERVER__*_PASSWORD (recommended)" \
    "yaml" "config.yaml server.host_password/player_password"

  # --- AI backend ---------------------------------------------------------------
  aw_menu AW_PROVIDER "AI backend" \
    "Which AI backend will act as Dungeon Master?\n\ncompatible = local llama.cpp (or another OpenAI-compatible server with structured-output support)\nopenai     = direct OpenAI API (needs an API key)" \
    "${AW_PROVIDER:-compatible}" \
    "compatible" "Local llama.cpp / OpenAI-compatible server" \
    "openai" "Direct OpenAI API"

  if [[ "$AW_PROVIDER" == "compatible" ]]; then
    AW_OPENAI_KEY=""
    while true; do
      aw_input AW_ENDPOINT "Model server endpoint" \
        "OpenAI-compatible base URL, including /v1.\n\nNOTE: 'localhost' means THIS container. If llama.cpp runs on another machine or the Proxmox host, use its LAN IP, e.g.\n  http://192.168.1.20:8033/v1\n\nAnyWorld also uses llama.cpp's /props, /apply-template and /tokenize endpoints when available." \
        "${AW_ENDPOINT:-http://localhost:8033/v1}" v_url
      if [[ "$AW_ENDPOINT" =~ ://(localhost|127\.0\.0\.1)([:/]|$) ]]; then
        local _local="no"
        aw_yesno _local "Endpoint is localhost" \
          "The endpoint points at localhost, i.e. inside this container.\n\nIs your model server actually running INSIDE container ${CTID}?\n\nChoose No to enter a different address." no
        if [[ "$_local" == "no" ]]; then continue; fi
      fi
      break
    done
    aw_input AW_MODEL "Model name" \
      "model_name sent to the server. llama.cpp generally accepts any value.\n\nDefault: local" \
      "${AW_MODEL:-local}" v_nospace
    aw_input AW_LLM_API_KEY "Model server API key" \
      "API key for the compatible server (llama.cpp ignores it unless started with --api-key).\n\nDefault: sk-no-key-required" \
      "${AW_LLM_API_KEY:-sk-no-key-required}" v_nospace
    aw_input AW_CTX "Context window (fallback)" \
      "context_window_size in tokens.\n\nFor llama.cpp this is only a FALLBACK: auto-detection via /props takes precedence when available. It cannot raise the backend's real capacity.\n\nDefault: 8192 (conservative)" \
      "${AW_CTX:-8192}" v_ctx
  else
    AW_ENDPOINT=""
    AW_LLM_API_KEY=""
    aw_secret AW_OPENAI_KEY "OpenAI API key" \
      "OpenAI API key (stored only in .env as AD_OPENAI_API_KEY)" no
    aw_input AW_MODEL "OpenAI model" \
      "OpenAI model name.\n\nAnyWorld has been tested live with gpt-5.6-luna.\n\nDefault: gpt-5.6-luna" \
      "${AW_MODEL:-gpt-5.6-luna}" v_nospace
    aw_input AW_CTX "Context window size" \
      "context_window_size in tokens (OpenAI has no auto-discovery).\n\ngpt-5.6-luna supports ~1.05M tokens, but per config.example.yaml pricing roughly doubles past 272k (278528) tokens. 262144 keeps compaction below that threshold.\n\nDefault: 262144" \
      "${AW_CTX:-262144}" v_ctx
  fi

  aw_menu AW_REASONING "Reasoning effort" \
    "Shared thinking effort (reasoning_effort).\n\nlow/medium/high reserve 2048/4096/8192 extra completion tokens per request. For llama.cpp, support depends on the loaded model and chat template." \
    "${AW_REASONING:-none}" \
    "none" "No thinking (default)" \
    "low" "Low effort" \
    "medium" "Medium effort" \
    "high" "High effort"

  # --- TLS ---------------------------------------------------------------------
  local _tls_default="${AW_TLS:-}" _dhcp_note=""
  if [[ -z "$_tls_default" ]]; then
    if [[ -n "$CT_IP" ]]; then _tls_default="${CT_IP},127.0.0.1"; else _tls_default="127.0.0.1"; fi
  fi
  if [[ "${NET:-dhcp}" == "dhcp" ]]; then
    _dhcp_note="\n\nThe container uses DHCP: add a DHCP reservation so this IP (and the certificate) stays valid."
  fi
  aw_input AW_TLS "HTTPS certificate addresses" \
    "AnyWorld serves HTTPS with a self-signed certificate. Left alone, it detects your PUBLIC IP, which inside an LXC usually doesn't match the LAN address players use.\n\nComma-separated IPs/hostnames for the certificate (add the hostname/FQDN, or your public IP for internet play).${_dhcp_note}" \
    "$_tls_default" v_tls

  # --- Advanced (optional) ----------------------------------------------------------
  AW_BIND_HOST="0.0.0.0"
  AW_TOKENIZER="cl100k_base"
  AW_INIT_OUT="" AW_ROUND_OUT="" AW_DICE_OUT="" AW_SUMMARY_OUT="" AW_SAFETY=""
  AW_TIMEOUT="" AW_RETRIES="" AW_COMPACTION="" AW_HISTORY=""
  AW_MAX_PENDING="" AW_AUTH_TIMEOUT="" AW_MAX_AUTH="" AW_DEBUG_RAW="no"
  aw_yesno AW_ADV "Advanced settings (optional)" \
    "Configure optional settings?\n\n- bind address and tokenizer\n- output token caps, timeouts, retries, compaction\n- connection admission limits\n- raw model-response debug logging\n\nChoose No to keep AnyWorld's defaults." no
  if [[ "$AW_ADV" == "yes" ]]; then
    aw_input AW_BIND_HOST "Bind address" \
      "Address the server listens on.\n0.0.0.0 = all interfaces (needed for LAN players).\n\nDefault: 0.0.0.0" "0.0.0.0" v_nospace
    aw_menu AW_TOKENIZER "Tokenizer encoding" \
      "tokenizer_encoding used for token estimates.\n\nFor OpenAI it is used only if it matches the model's known encoding. Choose null if estimates look wrong or the app errors on it (null = conservative UTF-8 byte estimate)." \
      "cl100k_base" \
      "cl100k_base" "Default" \
      "o200k_base" "Newer OpenAI tokenizer" \
      "null" "Conservative byte-based estimate"

    local _sub="no"
    aw_yesno _sub "LLM limits (optional)" \
      "Tune output token caps, timeouts, retries and memory compaction?\n\nChoose No to keep the defaults." no
    if [[ "$_sub" == "yes" ]]; then
      aw_input AW_INIT_OUT "initial_output_tokens" "Opening/scenario output cap.\nBlank = default (1024)" "" v_opt_posint
      aw_input AW_ROUND_OUT "round_output_tokens" "Round resolution output cap.\nBlank = default (2048)" "" v_opt_posint
      aw_input AW_DICE_OUT "dice_output_tokens" "Dice planning output cap.\nBlank = default (512)" "" v_opt_posint
      aw_input AW_SUMMARY_OUT "summary_output_tokens" "Summary/memory-audit output cap.\nBlank = default (1024)" "" v_opt_posint
      aw_input AW_SAFETY "token_safety_margin" "Extra tokens reserved per request.\nBlank = default (256)" "" v_opt_nonneg
      aw_input AW_TIMEOUT "request_timeout_seconds" "Per-request timeout. A round's overall deadline is 3x this value.\nBlank = default (120.0)\n\nSlow local models may need 180+." "" v_opt_float
      aw_input AW_RETRIES "max_retries" "Per-request repair retries.\nBlank = default (1)" "" v_opt_nonneg
      aw_input AW_COMPACTION "compaction_target_fraction" "After compaction starts, keep the next request within this fraction of the context window (0.5-1.0).\nBlank = default (0.75)" "" v_opt_fraction
      aw_input AW_HISTORY "history_round_limit" "Request earlier memory checkpoints after this many stored request/response pairs (2-100). Not a hard history cap.\nBlank = disabled (default)" "" v_opt_history
    fi

    _sub="no"
    aw_yesno _sub "Connection admission (optional)" \
      "Tune connection admission limits (pending sockets, auth timeout, auth attempts)?\n\nChoose No to keep the defaults." no
    if [[ "$_sub" == "yes" ]]; then
      aw_input AW_MAX_PENDING "max_pending_connections" "Blank = default (32)" "" v_opt_posint
      aw_input AW_AUTH_TIMEOUT "auth_timeout_seconds" "Blank = default (30.0)" "" v_opt_float
      aw_input AW_MAX_AUTH "max_auth_attempts" "Blank = default (3)" "" v_opt_posint
    fi

    aw_yesno AW_DEBUG_RAW "Raw response logging" \
      "Save every raw model HTTP response under .debug/llm/ (debug_raw_responses)?\n\nThese files can contain hidden dice and private DM guidance and are NOT rotated. Only enable while diagnosing." no
  fi

  # --- Extras -------------------------------------------------------------------
  aw_yesno AW_RUN_QC "Quality checks" \
    "Install the dev extras and run the INSTALL.md quality checks?\n\n  black --check, flake8, pytest, node client test\n\nTests use fake model clients; no LLM is needed. Adds a few minutes. Failures are reported but do not stop the install." yes
  AW_INSTALL_NODE="no"
  if [[ "$AW_RUN_QC" == "yes" ]]; then
    aw_yesno AW_INSTALL_NODE "Node.js" \
      "Install Node.js for the client reconnect / password-hashing regression test (node --test)?" yes
  fi
  aw_yesno AW_SERVICE "Systemd service" \
    "Install AnyWorld as a systemd service (runs as user 'anyworld', starts on boot)?\n\nNote: one server process hosts one game. Restart the service to begin a new game." yes
  AW_START_NOW="no"
  if [[ "$AW_SERVICE" == "yes" ]]; then
    aw_yesno AW_START_NOW "Start now" "Start the AnyWorld service when setup finishes?" yes
  fi
  AW_RUN_BENCH="no"
  if [[ "$AW_PROVIDER" == "compatible" ]]; then
    aw_yesno AW_RUN_BENCH "Model benchmark (optional)" \
      "Run benchmarks/benchmark_chance_events.py against your model now?\n\n40 live trials testing instruction following. Requires the model server to be reachable and can take a while. The report is saved under /opt/anyworld/benchmarks/." no
  fi
}

aw_summary_text() {
  local pw_note="" adv="defaults"
  if [[ "$AW_HOST_PW_GEN" == "yes" || "$AW_PLAYER_PW_GEN" == "yes" ]]; then
    pw_note=" (auto-generated values shown at the end)"
  fi
  if [[ "$AW_ADV" == "yes" ]]; then adv="customized"; fi
  local backend="$AW_PROVIDER, model ${AW_MODEL}"
  if [[ "$AW_PROVIDER" == "compatible" ]]; then backend+="\n             endpoint ${AW_ENDPOINT}"; fi
  printf '%s' "Server:      port ${AW_PORT}, max players ${AW_MAX_PLAYERS}, bind ${AW_BIND_HOST}
Passwords:   stored in $([[ "$AW_SECRET_STORE" == "env" ]] && echo ".env" || echo "config.yaml")${pw_note}
Backend:     ${backend}
Context:     ${AW_CTX} tokens, reasoning ${AW_REASONING}, tokenizer ${AW_TOKENIZER}
TLS cert:    ${AW_TLS}
Advanced:    ${adv}
Checks:      quality checks ${AW_RUN_QC}, Node.js ${AW_INSTALL_NODE}
Service:     install ${AW_SERVICE}, start now ${AW_START_NOW}
Benchmark:   ${AW_RUN_BENCH}
Repository:  ${AW_REPO} (${AW_BRANCH})"
}

aw_run_wizard() {
  local rc
  while true; do
    aw_wizard
    rc=0
    whiptail --backtitle "$AW_BT" --title "Confirm AnyWorld settings" --scrolltext \
      --yes-button "Install" --no-button "Start over" \
      --yesno "$(aw_summary_text)\n\nInstall AnyWorld with these settings?" 26 80 || rc=$?
    case "$rc" in
    0) return 0 ;;
    1) continue ;;
    *) aw_abort ;;
    esac
  done
}

# ==============================================================================
# Files pushed into the container
# ==============================================================================
aw_write_answers() {
  local f="$1"
  : >"$f"
  chmod 600 "$f"
  aw_put() { printf '%s=%s\n' "$1" "$(printf '%s' "${2:-}" | base64 -w0)" >>"$f"; }
  aw_put REPO "$AW_REPO"
  aw_put BRANCH "$AW_BRANCH"
  aw_put PORT "$AW_PORT"
  aw_put MAX_PLAYERS "$AW_MAX_PLAYERS"
  aw_put BIND_HOST "$AW_BIND_HOST"
  aw_put HOST_PW "$AW_HOST_PW"
  aw_put PLAYER_PW "$AW_PLAYER_PW"
  aw_put SECRET_STORE "$AW_SECRET_STORE"
  aw_put PROVIDER "$AW_PROVIDER"
  aw_put ENDPOINT "$AW_ENDPOINT"
  aw_put LLM_API_KEY "$AW_LLM_API_KEY"
  aw_put OPENAI_KEY "$AW_OPENAI_KEY"
  aw_put MODEL "$AW_MODEL"
  aw_put CTX "$AW_CTX"
  aw_put REASONING "$AW_REASONING"
  aw_put TOKENIZER "$AW_TOKENIZER"
  aw_put TLS_ADDRESSES "$AW_TLS"
  aw_put INIT_OUT "$AW_INIT_OUT"
  aw_put ROUND_OUT "$AW_ROUND_OUT"
  aw_put DICE_OUT "$AW_DICE_OUT"
  aw_put SUMMARY_OUT "$AW_SUMMARY_OUT"
  aw_put SAFETY "$AW_SAFETY"
  aw_put TIMEOUT "$AW_TIMEOUT"
  aw_put RETRIES "$AW_RETRIES"
  aw_put COMPACTION "$AW_COMPACTION"
  aw_put HISTORY_LIMIT "$AW_HISTORY"
  aw_put MAX_PENDING "$AW_MAX_PENDING"
  aw_put AUTH_TIMEOUT "$AW_AUTH_TIMEOUT"
  aw_put MAX_AUTH_ATTEMPTS "$AW_MAX_AUTH"
  aw_put DEBUG_RAW "$AW_DEBUG_RAW"
  aw_put RUN_QC "$AW_RUN_QC"
  aw_put INSTALL_NODE "$AW_INSTALL_NODE"
  aw_put SERVICE "$AW_SERVICE"
  aw_put START_NOW "$AW_START_NOW"
  aw_put RUN_BENCH "$AW_RUN_BENCH"
}

aw_write_config_builder() {
  cat >"$1" <<'PYEOF'
#!/usr/bin/env python3
"""Build AnyWorld config.yaml and .env from installer answers (base64 values)."""
import base64
import datetime
import pathlib
import sys

import yaml

ans_path, repo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
ans = {}
for raw in ans_path.read_text(encoding="utf-8").splitlines():
    if "=" in raw:
        key, val = raw.split("=", 1)
        ans[key] = base64.b64decode(val).decode("utf-8") if val else ""


def get(key, default=""):
    return ans.get(key, "") or default


def opt(section, key, name, cast):
    value = get(name)
    if value != "":
        section[key] = cast(value)


example = repo / "config.example.yaml"
cfg = {}
if example.exists():
    cfg = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
else:
    print("WARNING: config.example.yaml not found; system_prompt will use app defaults",
          file=sys.stderr)
server = cfg.get("server") or {}
llm = cfg.get("llm") or {}
cfg["server"], cfg["llm"] = server, llm

# --- server ---------------------------------------------------------------------
server["host"] = get("BIND_HOST", "0.0.0.0")
server["port"] = int(get("PORT", "4141"))
server["max_players"] = int(get("MAX_PLAYERS", "6"))
secret_store = get("SECRET_STORE", "env")
if secret_store == "yaml":
    server["host_password"] = get("HOST_PW")
    server["player_password"] = get("PLAYER_PW")
else:
    # Supplied through AD_SERVER__HOST_PASSWORD / AD_SERVER__PLAYER_PASSWORD in .env
    server["host_password"] = None
    server["player_password"] = None
opt(server, "max_pending_connections", "MAX_PENDING", int)
opt(server, "auth_timeout_seconds", "AUTH_TIMEOUT", float)
opt(server, "max_auth_attempts", "MAX_AUTH_ATTEMPTS", int)

# --- llm --------------------------------------------------------------------------
provider = get("PROVIDER", "compatible")
llm["provider"] = provider
llm["model_name"] = get("MODEL", "local")
if provider == "compatible":
    llm["endpoint"] = get("ENDPOINT", "http://localhost:8033/v1")
    llm["api_key"] = get("LLM_API_KEY", "sk-no-key-required")
llm["context_window_size"] = int(get("CTX", "8192"))
tokenizer = get("TOKENIZER", "cl100k_base")
llm["tokenizer_encoding"] = None if tokenizer == "null" else tokenizer
llm["reasoning_effort"] = get("REASONING", "none")
opt(llm, "initial_output_tokens", "INIT_OUT", int)
opt(llm, "round_output_tokens", "ROUND_OUT", int)
opt(llm, "dice_output_tokens", "DICE_OUT", int)
opt(llm, "summary_output_tokens", "SUMMARY_OUT", int)
opt(llm, "token_safety_margin", "SAFETY", int)
opt(llm, "request_timeout_seconds", "TIMEOUT", float)
opt(llm, "max_retries", "RETRIES", int)
opt(llm, "compaction_target_fraction", "COMPACTION", float)
opt(llm, "history_round_limit", "HISTORY_LIMIT", int)
if get("DEBUG_RAW") == "yes":
    llm["debug_raw_responses"] = True
# Keep the (long) game instructions last for readability.
if "system_prompt" in llm:
    llm["system_prompt"] = llm.pop("system_prompt")
else:
    print("WARNING: no system_prompt in config.example.yaml", file=sys.stderr)


class Dumper(yaml.SafeDumper):
    pass


def _str(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str)

stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
header = (
    f"# AnyWorld configuration - generated by the Proxmox LXC installer ({stamp}).\n"
    "# Based on config.example.yaml. AD_* environment variables (.env or the\n"
    "# service environment) take precedence over values in this file.\n"
    "# Apply changes with:  systemctl restart anyworld\n"
)
(repo / "config.yaml").write_text(
    header + yaml.dump(cfg, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=100),
    encoding="utf-8",
)

lines = ["# AnyWorld secrets - generated by the Proxmox LXC installer. Keep private."]
if secret_store != "yaml":
    lines.append(f"AD_SERVER__HOST_PASSWORD='{get('HOST_PW')}'")
    lines.append(f"AD_SERVER__PLAYER_PASSWORD='{get('PLAYER_PW')}'")
if provider == "openai":
    lines.append(f"AD_OPENAI_API_KEY='{get('OPENAI_KEY')}'")
(repo / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("config.yaml and .env written")
PYEOF
}

aw_write_install_script() {
  cat >"$1" <<'AWEOF'
#!/usr/bin/env bash
# Runs INSIDE the container. Installs and configures AnyWorld.
set -Eeuo pipefail
ANS="/root/anyworld-answers.env"
SUMMARY="/root/aw-summary.txt"
AW_DIR="/opt/anyworld"
AW_USER="anyworld"
AW_HOME="/var/lib/anyworld"
trap 'rm -f "$ANS" /root/aw-build-config.py' EXIT

get() {
  local v
  v="$(grep -m1 "^$1=" "$ANS" | cut -d= -f2- || true)"
  if [[ -n "$v" ]]; then printf '%s' "$v" | base64 -d; fi
}
step() { echo; echo "==> $*"; }
note() { echo "$*" | tee -a "$SUMMARY"; }
: >"$SUMMARY"

# Run a command as the service user from the repo root.
#   as_aw     -> with .env loaded (real runtime environment)
#   as_aw_raw -> without .env (quality checks use isolated settings)
_as() {
  local load_env="$1" cmd="$2"
  runuser -u "$AW_USER" -- env -i HOME="$AW_HOME" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
    TIKTOKEN_CACHE_DIR="$AW_HOME/tiktoken" PATH="$AW_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    AW_LOAD_ENV="$load_env" \
    bash -c 'cd /opt/anyworld && if [ "$AW_LOAD_ENV" = 1 ] && [ -f .env ]; then set -a; . ./.env; set +a; fi; eval "$1"' _ "$cmd"
}
as_aw() { _as 1 "$1"; }
as_aw_raw() { _as 0 "$1"; }

REPO="$(get REPO)"
BRANCH="$(get BRANCH)"
PROVIDER="$(get PROVIDER)"
PORT="$(get PORT)"
BIND_HOST="$(get BIND_HOST)"
MODEL="$(get MODEL)"
ENDPOINT="$(get ENDPOINT)"
TOKENIZER="$(get TOKENIZER)"
RUN_QC="$(get RUN_QC)"
INSTALL_NODE="$(get INSTALL_NODE)"
SERVICE="$(get SERVICE)"
START_NOW="$(get START_NOW)"
RUN_BENCH="$(get RUN_BENCH)"
export DEBIAN_FRONTEND=noninteractive

# --- Packages -------------------------------------------------------------------
step "Installing system packages"
pkgs=(git python3 python3-venv python3-pip ca-certificates curl openssl)
if [[ "$INSTALL_NODE" == "yes" ]]; then pkgs+=(nodejs); fi
apt-get update -q
apt-get install -y -q --no-install-recommends "${pkgs[@]}"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"AnyWorld needs Python 3.11+, found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} OK")
PY

# --- Service account ---------------------------------------------------------------
step "Creating service account '${AW_USER}'"
if ! id "$AW_USER" >/dev/null 2>&1; then
  useradd --system --user-group --create-home --home-dir "$AW_HOME" \
    --shell /usr/sbin/nologin "$AW_USER"
fi
install -d -o "$AW_USER" -g "$AW_USER" -m 750 "$AW_HOME" "$AW_HOME/tiktoken"

# --- Source -------------------------------------------------------------------
step "Fetching ${REPO} (${BRANCH})"
git config --global --add safe.directory "$AW_DIR"
if [[ -d "$AW_DIR/.git" ]]; then
  git -C "$AW_DIR" fetch --quiet origin "$BRANCH"
  git -C "$AW_DIR" checkout --quiet "$BRANCH"
  git -C "$AW_DIR" pull --quiet --ff-only origin "$BRANCH"
else
  git clone --quiet --branch "$BRANCH" "$REPO" "$AW_DIR"
fi
note "INFO  AnyWorld commit $(git -C "$AW_DIR" rev-parse --short HEAD) from ${REPO}"

# --- Python environment --------------------------------------------------------------
step "Creating virtual environment and installing AnyWorld"
cd "$AW_DIR"
python3 -m venv "$AW_DIR/venv"
"$AW_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
if [[ "$RUN_QC" == "yes" ]]; then
  "$AW_DIR/venv/bin/python" -m pip install --quiet -e '.[dev]'
else
  "$AW_DIR/venv/bin/python" -m pip install --quiet -e .
fi

# --- Configuration -------------------------------------------------------------
step "Writing config.yaml and .env"
"$AW_DIR/venv/bin/python" /root/aw-build-config.py "$ANS" "$AW_DIR"

args=()
IFS=',' read -r -a addrs <<<"$(get TLS_ADDRESSES)"
for a in "${addrs[@]}"; do
  a="${a//[[:space:]]/}"
  if [[ -n "$a" ]]; then args+=("--tls-address" "$a"); fi
done
cat >/etc/default/anyworld <<EOF
# Extra command-line arguments for AnyWorld (python app.py --help).
# --tls-address entries set the self-signed certificate's IPs/hostnames
# and skip public-IP discovery. Restart after editing:
#   systemctl restart anyworld
ANYWORLD_ARGS="${args[*]}"
EOF

chown -R "$AW_USER:$AW_USER" "$AW_DIR"
chmod 600 "$AW_DIR/config.yaml" "$AW_DIR/.env"

step "Validating configuration"
if as_aw 'python -B -c "from core.config import settings; settings.server.validate_passwords(); print(\"provider:\", settings.llm.provider, \"| model:\", settings.llm.model_name, \"| port:\", settings.server.port)"'; then
  note "PASS  config.yaml loads and passwords validate"
else
  note "FAIL  configuration validation (see output above)"
  exit 1
fi

# --- Tokenizer cache (tiktoken downloads encodings on first use) ------------------
if [[ -n "$TOKENIZER" && "$TOKENIZER" != "null" ]]; then
  step "Pre-caching tokenizer ${TOKENIZER}"
  if as_aw_raw "python -B -c 'import tiktoken; tiktoken.get_encoding(\"${TOKENIZER}\")'"; then
    note "PASS  tokenizer ${TOKENIZER} cached"
  else
    note "WARN  could not pre-cache ${TOKENIZER}; it will download on first use"
  fi
fi

# --- Backend reachability (informational) -----------------------------------------
step "Checking AI backend"
if [[ "$PROVIDER" == "compatible" ]]; then
  base="${ENDPOINT%/}"
  if curl -fsS -m 8 -o /dev/null "${base}/models"; then
    note "PASS  model server reachable at ${base}"
    if curl -fsS -m 8 -o /dev/null "${base%/v1}/props"; then
      note "PASS  llama.cpp /props available (context size auto-detected)"
    else
      note "INFO  /props not available; configured context_window_size will be used"
    fi
  else
    note "WARN  model server not reachable at ${base} - start it before hosting a game"
  fi
else
  hdr="$(mktemp)"
  chmod 600 "$hdr"
  printf 'Authorization: Bearer %s\n' "$(get OPENAI_KEY)" >"$hdr"
  code="$(curl -s -m 15 -o /dev/null -w '%{http_code}' -H @"$hdr" https://api.openai.com/v1/models || true)"
  rm -f "$hdr"
  case "$code" in
  200) note "PASS  OpenAI API key accepted" ;;
  401) note "WARN  OpenAI rejected the API key (HTTP 401) - fix AD_OPENAI_API_KEY in ${AW_DIR}/.env" ;;
  *) note "WARN  could not verify the OpenAI API key (HTTP ${code:-none})" ;;
  esac
fi

# --- Quality checks (INSTALL.md#quality-checks) ------------------------------------
if [[ "$RUN_QC" == "yes" ]]; then
  step "Running quality checks"
  QC_LOG="$AW_DIR/quality-checks.log"
  : >"$QC_LOG"
  chown "$AW_USER:$AW_USER" "$QC_LOG"
  qc() {
    local name="$1" cmd="$2"
    echo "### ${name}: ${cmd}" >>"$QC_LOG"
    if as_aw_raw "$cmd" >>"$QC_LOG" 2>&1; then
      note "PASS  ${name}"
    else
      note "FAIL  ${name} (details: ${QC_LOG})"
    fi
  }
  qc "black --check" "black --check app.py api core logic tests"
  qc "flake8" "flake8 app.py api core logic tests"
  qc "pytest" "python -B -m pytest -p no:cacheprovider"
  if command -v node >/dev/null 2>&1; then
    qc "node client tests" "node --test tests/client_reconnect.test.cjs"
  else
    note "SKIP  node client tests (Node.js not installed)"
  fi
fi

# --- Update helper --------------------------------------------------------------
cat >/usr/local/bin/anyworld-update <<'EOF'
#!/usr/bin/env bash
# Pull the latest AnyWorld, reinstall dependencies, restart the service.
set -Eeuo pipefail
AW_DIR=/opt/anyworld
as_aw() { runuser -u anyworld -- env HOME=/var/lib/anyworld "$@"; }
as_aw git -C "$AW_DIR" pull --ff-only
extras=""
if [[ -x "$AW_DIR/venv/bin/pytest" ]]; then extras="[dev]"; fi
cd "$AW_DIR"
as_aw "$AW_DIR/venv/bin/python" -m pip install --quiet -e ".${extras}"
if systemctl is-enabled --quiet anyworld 2>/dev/null; then systemctl restart anyworld; fi
echo "AnyWorld updated to $(as_aw git -C "$AW_DIR" rev-parse --short HEAD)"
EOF
chmod 755 /usr/local/bin/anyworld-update

# --- systemd service ------------------------------------------------------------
if [[ "$SERVICE" == "yes" ]]; then
  step "Installing systemd service"
  cat >/etc/systemd/system/anyworld.service <<'EOF'
[Unit]
Description=AnyWorld - AI dungeon master multiplayer text RPG
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=anyworld
Group=anyworld
WorkingDirectory=/opt/anyworld
EnvironmentFile=-/opt/anyworld/.env
EnvironmentFile=-/etc/default/anyworld
Environment=HOME=/var/lib/anyworld
Environment=PYTHONUNBUFFERED=1
Environment=TIKTOKEN_CACHE_DIR=/var/lib/anyworld/tiktoken
ExecStart=/opt/anyworld/venv/bin/python app.py $ANYWORLD_ARGS
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --quiet anyworld
  note "PASS  systemd service installed and enabled"

  if [[ "$START_NOW" == "yes" ]]; then
    step "Starting AnyWorld"
    systemctl restart anyworld
    probe="127.0.0.1"
    if [[ -n "$BIND_HOST" && "$BIND_HOST" != "0.0.0.0" && "$BIND_HOST" != "::" ]]; then probe="$BIND_HOST"; fi
    ok="no"
    code=""
    for _ in $(seq 1 30); do
      code="$(curl -sk -m 3 -o /dev/null -w '%{http_code}' "https://${probe}:${PORT}/" || true)"
      if [[ "$code" =~ ^[23] ]]; then ok="yes"; break; fi
      sleep 2
    done
    if [[ "$ok" == "yes" ]]; then
      note "PASS  service is answering on https://${probe}:${PORT}/ (HTTP ${code})"
    else
      note "FAIL  service did not answer on port ${PORT}; last log lines:"
      journalctl -u anyworld -n 25 --no-pager >>"$SUMMARY" 2>&1 || true
    fi
  fi
fi

# --- Optional model benchmark ----------------------------------------------------
if [[ "$RUN_BENCH" == "yes" ]]; then
  step "Running chance-event benchmark (this can take a while)"
  safe="$(printf '%s' "$MODEL" | tr -c 'A-Za-z0-9._-' '_')"
  out="benchmarks/model-bench-${safe}.json"
  BLOG="$AW_DIR/benchmark.log"
  : >"$BLOG"
  chown "$AW_USER:$AW_USER" "$BLOG"
  if as_aw "timeout 3600 python -B benchmarks/benchmark_chance_events.py --output ${out}" >>"$BLOG" 2>&1; then
    note "PASS  benchmark: $(grep -E 'overall=' "$BLOG" | tail -n 1 || echo 'finished')"
    note "INFO  benchmark report: ${AW_DIR}/${out}"
  else
    note "WARN  benchmark failed or timed out (details: ${BLOG})"
  fi
fi

echo
echo "AnyWorld install finished."
AWEOF
}

# ==============================================================================
# Install AnyWorld into the container
# ==============================================================================
aw_install() {
  AW_TMP="$(mktemp -d)"
  chmod 700 "$AW_TMP"
  aw_write_answers "$AW_TMP/answers.env"
  aw_write_config_builder "$AW_TMP/aw-build-config.py"
  aw_write_install_script "$AW_TMP/aw-install.sh"

  pct push "$CTID" "$AW_TMP/answers.env" /root/anyworld-answers.env --perms 600
  pct push "$CTID" "$AW_TMP/aw-build-config.py" /root/aw-build-config.py --perms 600
  pct push "$CTID" "$AW_TMP/aw-install.sh" /root/aw-install.sh --perms 700
  rm -rf "$AW_TMP"
  AW_TMP=""

  AW_LOG="/tmp/anyworld-install-${CTID}.log"
  local rc=0
  if [[ "${VERBOSE:-no}" == "yes" ]]; then
    echo -e "${INFO}${YW} Installing AnyWorld in CT ${CTID}...${CL}"
    pct exec "$CTID" -- bash /root/aw-install.sh 2>&1 | tee "$AW_LOG" || rc=$?
  else
    msg_info "Installing AnyWorld in CT ${CTID} (this can take several minutes)"
    pct exec "$CTID" -- bash /root/aw-install.sh >"$AW_LOG" 2>&1 || rc=$?
  fi

  if [[ "$rc" -ne 0 ]]; then
    aw_cleanup
    msg_error "AnyWorld installation failed (exit ${rc}). Last lines of ${AW_LOG}:"
    tail -n 40 "$AW_LOG" || true
    exit 1
  fi
  msg_ok "Installed AnyWorld (full log: ${AW_LOG})"

  echo -e "\n${INFO}${BOLD}${DGN}Setup results:${CL}"
  pct exec "$CTID" -- cat /root/aw-summary.txt 2>/dev/null | sed 's/^/   /' || true
  pct exec "$CTID" -- rm -f /root/aw-install.sh /root/aw-summary.txt >/dev/null 2>&1 || true
  echo
}

aw_final_message() {
  local ip="${CT_IP:-<container-ip>}"
  echo -e "${INFO}${YW} Open AnyWorld (the host signs in first):${CL}"
  echo -e "${TAB}${GATEWAY}${BGN}https://${ip}:${AW_PORT}/${CL}"
  echo -e "${INFO}${YW} Browsers will warn about the self-signed certificate. Players can accept the"
  echo -e "${TAB}exception or import ${BGN}/opt/anyworld/certs/cert.pem${CL}${YW} (never share key.pem).${CL}"
  if [[ "$AW_HOST_PW_GEN" == "yes" ]]; then
    echo -e "${INFO}${YW} Generated host password:   ${BGN}${AW_HOST_PW}${CL}"
  fi
  if [[ "$AW_PLAYER_PW_GEN" == "yes" ]]; then
    echo -e "${INFO}${YW} Generated player password: ${BGN}${AW_PLAYER_PW}${CL}"
  fi
  if [[ "$AW_SECRET_STORE" == "env" ]]; then
    echo -e "${INFO}${YW} Passwords are stored in /opt/anyworld/.env${CL}"
  else
    echo -e "${INFO}${YW} Passwords are stored in /opt/anyworld/config.yaml${CL}"
  fi
  echo -e "${INFO}${YW} Useful commands inside the container (pct enter ${CTID}):${CL}"
  echo -e "${TAB}systemctl restart anyworld     ${YW}# start a new game / apply config changes${CL}"
  echo -e "${TAB}journalctl -u anyworld -f      ${YW}# live server log${CL}"
  echo -e "${TAB}anyworld-update                ${YW}# pull latest AnyWorld and restart${CL}"
}

# ==============================================================================
# Main
# ==============================================================================
start
build_container
aw_get_ct_ip
aw_run_wizard
aw_install
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
aw_final_message
