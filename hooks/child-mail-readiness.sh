# Shared by the delegated launchers. Standalone sessions have no parent.
CHILD_MAIL_ATTEMPT=""
CHILD_MAIL_PYTHON="${AGENTSTACK_PYTHON:-python3}"
CHILD_MAIL_HELPER="$(dirname "${BASH_SOURCE[0]}")/../integrations/codex_app/src/agentstack_codex_app/child_mail_readiness.py"

child_mail_begin() {
    local parent="$1" program="$2" token_file="$3"
    [[ -n "$parent" ]] || return 0
    if ! CHILD_MAIL_ATTEMPT="$("$CHILD_MAIL_PYTHON" "$CHILD_MAIL_HELPER" begin \
        "$RUNTIME_DIR" "$CHILD_NAME" "$PROJECT_KEY" "$program")"; then
        child_mail_fail "$parent" "$token_file" "could not prepare the ORRERY Mail startup check."
        return 1
    fi
    # All callers share the normal dashboard deadline. Count from the first
    # CLI launch, including its pre-existing prompt and trust checks.
    CHILD_MAIL_DEADLINE=$((SECONDS + 90))
}

child_mail_require_proxy() {
    [[ -z "$1" || -n "$3" ]] || child_mail_fail "$1" "$2" "the child has no bound ORRERY Mail proxy."
}

child_mail_prompt() {
    if [[ -n "$CHILD_MAIL_ATTEMPT" ]]; then
        printf 'ORRERY child startup check for %s (%s). First call the ORRERY Mail MCP send_message tool from the orrery-mail server with to=["%s"], subject="ORRERY child startup check", body_md="%s". Start the task below only after startup_confirmed=true. If the tool is unavailable or refuses the check, stop this turn.\n\n' \
            "$CHILD_NAME" "${CHILD_MAIL_ATTEMPT##*.mail-start-}" "$CHILD_NAME" "${CHILD_MAIL_ATTEMPT##*.mail-start-}"
    fi
    printf '%s' "$1"
}

child_mail_end() {
    if [[ -n "$CHILD_MAIL_ATTEMPT" ]]; then
        rm -rf -- "$CHILD_MAIL_ATTEMPT"
        CHILD_MAIL_ATTEMPT=""
    fi
}

child_mail_confirmed() {
    [[ -n "$CHILD_MAIL_ATTEMPT" && -f "$CHILD_MAIL_ATTEMPT/verdict" ]] \
        && [[ "$(<"$CHILD_MAIL_ATTEMPT/verdict")" == ready ]]
}

child_mail_fail() {
    local parent="$1" token_file="$2" detail="$3" reason
    if [[ -n "$CHILD_MAIL_ATTEMPT" ]] \
        && ! "$CHILD_MAIL_PYTHON" "$CHILD_MAIL_HELPER" reject "$CHILD_MAIL_ATTEMPT" \
        && child_mail_confirmed; then
        return 0
    fi
    reason="ORRERY Mail startup check failed for $CHILD_NAME: $detail"
    echo "[spawn_child] mail startup failure: $CHILD_NAME: $reason" >&2
    if [[ -n "$parent" ]] && ! "$CHILD_MAIL_PYTHON" "$CHILD_MAIL_HELPER" notify "$token_file" "$MCP_URL" "$MAIL_ENV" \
        "$HTTP_BEARER_MODE" "$PROJECT_KEY" "$CHILD_NAME" "$parent" "$reason"; then
        echo "[spawn_child] ORRERY Mail could not deliver the startup failure to $parent." >&2
    fi
    return 1
}

child_mail_wait() {
    local parent="$1" token_file="$2" native_exit="${3:-}" detail=""
    [[ -n "$CHILD_MAIL_ATTEMPT" ]] || return 0
    while ! child_mail_confirmed; do
        if [[ -n "$native_exit" && -f "$native_exit" ]] || ! tmux has-session -t "=$CHILD_NAME" 2>/dev/null; then
            detail="the child exited before confirming orrery-mail send_message."
            break
        fi
        if (( SECONDS >= CHILD_MAIL_DEADLINE )); then
            detail="the first turn did not confirm orrery-mail send_message within 90 seconds."
            break
        fi
        sleep 0.25
    done
    if [[ -n "$detail" ]]; then
        child_mail_fail "$parent" "$token_file" "$detail" || return 1
    fi
    child_mail_end
}

child_mail_check_early_failure() {
    if [[ -n "$CHILD_MAIL_ATTEMPT" ]] && ! child_mail_confirmed; then
        child_mail_fail "$1" "$2" "${3:-the child could not start its first turn and confirm orrery-mail send_message.}" || :
    fi
}
